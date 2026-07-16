/*
 * Step 1 UDP connectivity proof for the Portenta H7 (M7).
 * See docs/UDP_stream_migration_plan.md.
 *
 * Brings up Ethernet with a static IP on the host's link-local /16 segment
 * (host is on 169.254.245.x APIPA, so NO host reconfig is needed), then
 * BROADCASTS a 1 Hz UDP heartbeat to 169.254.255.255:7777 — broadcast so it
 * reaches the PC regardless of the PC's exact auto-assigned address.
 *
 * The point of this test is two things:
 *   1. Does Ethernet + UDP work at all on this carrier? (PC receives heartbeats)
 *   2. Is EthernetUDP::endPacket() NON-BLOCKING? We time it every send and print
 *      endPacket_us + its running max. If it stays microseconds even when the PC
 *      isn't listening, the "fire-and-forget" premise holds and the full stream
 *      migration is safe. If it spikes to ms, we learn that here, cheaply.
 *
 * USB serial (COM8, 115200) prints diagnostics. This image has NO sensor/SMA
 * logic — flash the production firmware back when done.
 */
#include <Arduino.h>
#include <PortentaEthernet.h>
#include <Ethernet.h>
#include <EthernetUdp.h>

static IPAddress kH7Ip(169, 254, 245, 50);          // H7 static (host is .100)
static IPAddress kPcIp(169, 254, 245, 100);         // send straight to the PC.
// (Was a /16 broadcast, but Ethernet.begin(ip) defaults the H7 to a /24 subnet,
//  so 169.254.255.255 is off-subnet and gets dropped. Unicast to the PC — ping
//  proved that path works both ways — is the robust choice for a direct link.)
static const uint16_t kPort = 7777;

static EthernetUDP Udp;
static uint32_t seq = 0;
static uint32_t last_ms = 0;
static uint32_t send_max_us = 0;                    // worst endPacket() so far

static const char* linkStr() {
    switch (Ethernet.linkStatus()) {
        case LinkON:  return "ON";
        case LinkOFF: return "OFF";
        default:      return "unknown";
    }
}

void setup() {
    Serial.begin(115200);
    uint32_t t0 = millis();
    while (!Serial && millis() - t0 < 3000) { }
    Serial.println();
    Serial.println("[UDP-TEST] boot — bringing up Ethernet (static)...");

    Ethernet.begin(kH7Ip);                          // static IP, no DHCP wait
    Udp.begin(kPort);

    Serial.print("[UDP-TEST] H7 IP  : "); Serial.println(Ethernet.localIP());
    Serial.print("[UDP-TEST] link   : "); Serial.println(linkStr());
    Serial.print("[UDP-TEST] target : "); Serial.print(kPcIp);
    Serial.print(":"); Serial.println(kPort);
    Serial.println("[UDP-TEST] sending 1 Hz heartbeats — run udp_listen.py on the PC");
}

void loop() {
    uint32_t now = millis();
    if (now - last_ms < 1000) return;
    last_ms = now;

    char buf[96];
    int n = snprintf(buf, sizeof(buf), "H7-HEARTBEAT seq=%lu ms=%lu\n",
                     (unsigned long)seq, (unsigned long)now);

    uint32_t s0 = micros();
    Udp.beginPacket(kPcIp, kPort);
    Udp.write((const uint8_t*)buf, (size_t)n);
    Udp.endPacket();
    uint32_t dt = micros() - s0;                    // <-- the number that matters
    if (dt > send_max_us) send_max_us = dt;

    Serial.print("[UDP-TEST] seq="); Serial.print(seq);
    Serial.print(" endPacket_us="); Serial.print(dt);
    Serial.print(" (max="); Serial.print(send_max_us);
    Serial.print(") link="); Serial.println(linkStr());
    seq++;
}
