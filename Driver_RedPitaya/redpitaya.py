"""
redpitaya.py -- thin SCPI driver for the Red Pitaya STEMlab 125-14.

Philosophy
----------
This module is deliberately *dumb about impedance*. It exposes only what the
board physically does:

    1. Generate a sine on a fast output (OUT1/OUT2).
    2. Acquire raw voltage waveforms on the fast inputs (IN1/IN2), captured
       *phase-coherently* with the generated tone.

Everything above that -- turning two voltage buffers into a complex impedance,
de-embedding the fixture, fitting R and L across frequency -- lives on the host
in a separate processing layer. Keeping that math out of the driver is the whole
point: you own the calibration and the model, not the board.

The single primitive you call is ``RedPitaya.capture(freq, ampl)``, which
returns a ``Capture`` holding two NumPy voltage arrays plus the metadata a
host-side DFT needs (effective sample rate, exact generated frequency, length).

Transport
---------
Talks to the Red Pitaya SCPI server over TCP (default port 5000). No external
SCPI library is required -- a minimal socket client is built in so this module
has no dependency beyond the standard library and NumPy. The SCPI server must
be running on the board (start it from the Red Pitaya web interface or via
``systemctl start redpitaya_scpi``).

Requires Red Pitaya OS 2.00 or newer for the AWG-triggered acquisition and
buffer-fill polling used here.

NOTE: This driver has been written against the documented SCPI command set but
should be validated against real hardware before production use -- in
particular the binary-transfer path and the trigger-delay value (see capture()).

Author: drafted for the SMA-coil impedance characterisation work, HDR Lab.
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

LOG = logging.getLogger("redpitaya")

# ---------------------------------------------------------------------------
# Board constants (STEMlab 125-14)
# ---------------------------------------------------------------------------
ADC_BUFFER = 16384                # samples per channel per acquisition
BASE_FS = 125_000_000.0           # base ADC/DAC sample rate (Hz)
VALID_DECIMATIONS = (
    1, 2, 4, 8, 16, 32, 64, 128, 256, 512,
    1024, 2048, 4096, 8192, 16384, 32768, 65536,
)


class RedPitayaError(RuntimeError):
    """Raised on connection loss, SCPI protocol errors, or capture timeouts."""


# ---------------------------------------------------------------------------
# Minimal SCPI socket client
# ---------------------------------------------------------------------------
class _Scpi:
    """Bare-bones line/block SCPI client over a raw TCP socket.

    Handles the two response shapes the Red Pitaya server uses:
      - text responses terminated by CR/LF (``rx_txt``)
      - SCPI definite-length binary blocks ``#<ndig><len><payload>`` (``rx_bin``)
    """

    TERM = b"\r\n"

    def __init__(self, host: str, port: int = 5000, timeout: float = 10.0):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buf = bytearray()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    # -- low-level --------------------------------------------------------
    def _fill(self, nbytes: int = 65536) -> None:
        chunk = self._sock.recv(nbytes)
        if not chunk:
            raise RedPitayaError("SCPI connection closed by peer")
        self._buf.extend(chunk)

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    # -- public -----------------------------------------------------------
    def tx(self, cmd: str) -> None:
        self._sock.sendall(cmd.encode("ascii") + self.TERM)

    def rx_txt(self) -> str:
        while self.TERM not in self._buf:
            self._fill()
        idx = self._buf.index(self.TERM)
        line = bytes(self._buf[:idx])
        del self._buf[: idx + len(self.TERM)]
        return line.decode("ascii", "replace")

    def txrx(self, cmd: str) -> str:
        self.tx(cmd)
        return self.rx_txt()

    def rx_bin(self) -> bytes:
        """Parse one SCPI definite-length binary block and return its payload."""
        # locate the leading '#', discarding any stray leading bytes
        while b"#" not in self._buf:
            self._fill()
        del self._buf[: self._buf.index(b"#")]
        self._recv_exact(1)                       # consume '#'
        ndig = int(self._recv_exact(1).decode())
        length = int(self._recv_exact(ndig).decode())
        payload = self._recv_exact(length)
        # consume the trailing CR/LF the server appends after the block
        trailer = self._recv_exact(2)
        if trailer != self.TERM:
            # not fatal, but worth knowing if framing drifts
            LOG.debug("unexpected binary-block trailer: %r", trailer)
        return payload


# ---------------------------------------------------------------------------
# Capture result
# ---------------------------------------------------------------------------
@dataclass
class Capture:
    """One phase-coherent dual-channel acquisition. Pure voltages, no physics."""

    freq: float                  # exact frequency actually generated (bin-snapped)
    requested_freq: float        # frequency the caller asked for
    ampl: float                  # generator amplitude (V)
    fs: float                    # effective sample rate after decimation (Hz)
    decimation: int
    n: int                       # samples per channel
    ch1: np.ndarray              # IN1 voltages (e.g. across the DUT, Kelvin sense)
    ch2: np.ndarray              # IN2 voltages (e.g. across the sense shunt)
    host_timestamp_s: float      # time.time() at read -- for cross-instrument joins
    monotonic_s: float           # time.monotonic() at read -- drift-free timing

    @property
    def t(self) -> np.ndarray:
        """Sample time vector (s), starting at 0."""
        return np.arange(self.n) / self.fs

    @property
    def periods(self) -> float:
        """Number of excitation periods inside the captured window."""
        return self.freq * self.n / self.fs


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
class RedPitaya:
    """SCPI driver exposing phase-coherent generate + acquire on a STEMlab 125-14.

    Typical use::

        with RedPitaya("rp-f0a235.local") as rp:
            cap = rp.capture(freq=1e6, ampl=0.5)
            # hand cap.ch1 / cap.ch2 to a host-side impedance processor
    """

    def __init__(
        self,
        host: str,
        port: int = 5000,
        timeout: float = 10.0,
        gen_channel: int = 1,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.gen_channel = gen_channel
        self._scpi: Optional[_Scpi] = None
        self.idn: Optional[str] = None

    # -- lifecycle --------------------------------------------------------
    def connect(self) -> "Driver_RedPitaya":
        self._scpi = _Scpi(self.host, self.port, self.timeout)
        self.idn = self._scpi.txrx("*IDN?").strip()
        LOG.info("connected: %s", self.idn)
        return self

    def close(self) -> None:
        if self._scpi is not None:
            try:
                # leave the output disabled so we never park drive on the DUT
                self._scpi.tx(f"OUTPUT{self.gen_channel}:STATE OFF")
                self._scpi.tx("ACQ:STOP")
            except (OSError, RedPitayaError):
                pass
            self._scpi.close()
            self._scpi = None

    def __enter__(self) -> "Driver_RedPitaya":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _require(self) -> _Scpi:
        if self._scpi is None:
            raise RedPitayaError("not connected; call .connect() first")
        return self._scpi

    def reset(self) -> None:
        scpi = self._require()
        scpi.tx("GEN:RST")
        scpi.tx("ACQ:RST")

    # -- planning helpers -------------------------------------------------
    @staticmethod
    def snap_frequency(freq: float, fs: float, n: int = ADC_BUFFER) -> float:
        """Snap ``freq`` to the nearest exact DFT bin for the given window.

        Landing on a bin means the capture holds an integer number of periods,
        so a single-bin DFT at this frequency is leakage-free with a plain
        rectangular window -- no windowing, no scalloping loss. The host should
        always analyse at the *returned* frequency, not the requested one.
        """
        k = max(1, round(freq * n / fs))
        return k * fs / n

    @staticmethod
    def auto_decimation(freq: float, target_periods: int = 100) -> int:
        """Smallest decimation whose 16384-sample window holds >= target periods.

        Higher decimation lengthens the time window (more periods, finer bin
        spacing) at the cost of a longer -- and slightly more self-heating --
        capture. For MHz-range tones dec=1 already gives plenty of periods.
        """
        chosen = VALID_DECIMATIONS[-1]
        for dec in VALID_DECIMATIONS:
            periods = freq * ADC_BUFFER * dec / BASE_FS
            if periods >= target_periods:
                chosen = dec
                break
        return chosen

    # -- the one primitive ------------------------------------------------
    def capture(
        self,
        freq: float,
        ampl: float,
        offset: float = 0.0,
        decimation: Optional[int] = None,
        binary: bool = False,
        settle_s: float = 0.0,
    ) -> Capture:
        """Generate a tone and capture IN1/IN2 phase-coherently with it.

        Parameters
        ----------
        freq : float
            Target excitation frequency (Hz). Internally snapped to the nearest
            DFT bin; the actual value is returned in ``Capture.freq``.
        ampl : float
            Generator amplitude (V). Respect your front-end limits -- with the
            input-divider-bypass jumpers fitted, keep amplitude+offset <= 0.5 V.
        offset : float
            Generator DC offset (V).
        decimation : int or None
            ADC decimation. If None, chosen automatically for ~100 periods.
        binary : bool
            If True, transfer samples as little-endian float32 (much faster on
            the wire). If False (default), use ASCII -- slower but maximally
            robust. Validate the binary path on your board before relying on it.
        settle_s : float
            Optional dwell after enabling the output and before triggering, to
            let the analog front-end settle. Keep small to limit self-heating.

        Returns
        -------
        Capture
            Raw voltages plus timing/metadata. No impedance is computed here.
        """
        scpi = self._require()
        ch = self.gen_channel

        dec = decimation if decimation is not None else self.auto_decimation(freq)
        if dec not in VALID_DECIMATIONS:
            raise ValueError(f"decimation {dec} not in {VALID_DECIMATIONS}")
        fs = BASE_FS / dec
        f_act = self.snap_frequency(freq, fs)

        # ---- configure generation (does not start until triggered) ----
        scpi.tx("GEN:RST")
        scpi.tx(f"SOUR{ch}:FUNC SINE")
        scpi.tx(f"SOUR{ch}:FREQ:FIX {f_act:.6f}")
        scpi.tx(f"SOUR{ch}:VOLT {ampl:.6f}")
        scpi.tx(f"SOUR{ch}:VOLT:OFFS {offset:.6f}")
        scpi.tx(f"SOUR{ch}:TRig:SOUR INT")
        scpi.tx(f"OUTPUT{ch}:STATE ON")        # arm output (init value appears)

        # ---- configure acquisition, armed on the AWG start edge ----
        scpi.tx("ACQ:RST")
        scpi.tx(f"ACQ:DEC {dec}")
        scpi.tx("ACQ:DATA:UNITS VOLTS")
        scpi.tx("ACQ:DATA:FORMAT " + ("BIN" if binary else "ASCII"))
        if binary:
            scpi.tx("ACQ:DATA:BYTE:ORDER LEND")
        # Push the trigger point to the *start* of the buffer so all 16384
        # samples are post-trigger (i.e. pure steady tone). Without this the
        # default trigger sits at sample 8192 and the first half of the buffer
        # is pre-generation baseline.
        scpi.tx(f"ACQ:TRig:DLY {ADC_BUFFER // 2}")
        scpi.tx("ACQ:START")
        scpi.tx("ACQ:TRig AWG_PE")             # wait for the generator's start edge

        if settle_s > 0:
            time.sleep(settle_s)

        # ---- fire generation -> AWG edge triggers the acquisition ----
        scpi.tx(f"SOUR{ch}:TRig:INT")
        self._wait_triggered(scpi)
        self._wait_filled(scpi)

        ts = time.time()
        mono = time.monotonic()
        v1 = self._read_channel(scpi, 1, binary)
        v2 = self._read_channel(scpi, 2, binary)

        # stop driving immediately -- the DUT is thermally sensitive
        scpi.tx("ACQ:STOP")
        scpi.tx(f"OUTPUT{ch}:STATE OFF")

        n = min(v1.size, v2.size)
        return Capture(
            freq=f_act,
            requested_freq=freq,
            ampl=ampl,
            fs=fs,
            decimation=dec,
            n=n,
            ch1=v1[:n],
            ch2=v2[:n],
            host_timestamp_s=ts,
            monotonic_s=mono,
        )

    def capture_sweep(
        self,
        freqs: Sequence[float],
        ampl: float,
        **kwargs,
    ) -> list[Capture]:
        """Capture at each frequency in turn. The host fits R, L across them."""
        return [self.capture(f, ampl, **kwargs) for f in freqs]

    # -- internals --------------------------------------------------------
    def _wait_triggered(self, scpi: _Scpi, timeout: float = 5.0) -> None:
        t0 = time.monotonic()
        while True:
            if scpi.txrx("ACQ:TRig:STAT?").strip() == "TD":
                return
            if time.monotonic() - t0 > timeout:
                raise RedPitayaError("acquisition trigger timeout (no AWG edge?)")
            time.sleep(0.001)

    def _wait_filled(self, scpi: _Scpi, timeout: float = 5.0) -> None:
        # OS 2.00+. After TD on a steady tone this completes almost immediately;
        # treat a timeout as non-fatal rather than discarding a good buffer.
        t0 = time.monotonic()
        while True:
            try:
                if scpi.txrx("ACQ:TRig:FILL?").strip() == "1":
                    return
            except RedPitayaError:
                return
            if time.monotonic() - t0 > timeout:
                LOG.warning("buffer-fill wait timed out; proceeding")
                return
            time.sleep(0.001)

    def _read_channel(self, scpi: _Scpi, n: int, binary: bool) -> np.ndarray:
        scpi.tx(f"ACQ:SOUR{n}:DATA?")
        if binary:
            payload = scpi.rx_bin()
            return np.frombuffer(payload, dtype="<f4").astype(np.float64)
        txt = scpi.rx_txt().strip().strip("{}").strip()
        if not txt:
            return np.empty(0, dtype=np.float64)
        return np.array(txt.split(","), dtype=np.float64)


# ---------------------------------------------------------------------------
# Smoke test (only runs when executed directly, never on import)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Red Pitaya capture smoke test")
    ap.add_argument("host", help="Red Pitaya hostname or IP, e.g. rp-f0a235.local")
    ap.add_argument("--freq", type=float, default=1e6)
    ap.add_argument("--ampl", type=float, default=0.5)
    ap.add_argument("--binary", action="store_true")
    args = ap.parse_args()

    with RedPitaya(args.host) as rp:
        cap = rp.capture(args.freq, args.ampl, binary=args.binary)
        print(f"IDN              : {rp.idn}")
        print(f"requested freq   : {cap.requested_freq:.3f} Hz")
        print(f"actual freq      : {cap.freq:.3f} Hz  (bin-snapped)")
        print(f"sample rate      : {cap.fs:.3e} Hz  (dec={cap.decimation})")
        print(f"samples / chan   : {cap.n}")
        print(f"periods captured : {cap.periods:.2f}")
        print(f"IN1 Vrms         : {np.sqrt(np.mean(cap.ch1**2)):.4e} V")
        print(f"IN2 Vrms         : {np.sqrt(np.mean(cap.ch2**2)):.4e} V")
