import struct
import unittest
from simnext_linux.p2p_transport.wire import relay_offer, relay_registration, peer_hello
from simnext_linux.p2p_transport.discovery import ProtocolError

class RelayTests(unittest.TestCase):
    def offer(self):
        packet=bytearray(588)
        struct.pack_into('<HHI',packet,0,0x2012,1004,17)
        packet[8:108]=b'DEVICE'.ljust(100,b'\0')
        packet[108:208]=b'CLIENT'.ljust(100,b'\0')
        packet[208:224]=b'192.0.2.2'.ljust(16,b'\0')
        struct.pack_into('<H',packet,224,4000)
        packet[332:348]=b'192.0.2.3'.ljust(16,b'\0')
        struct.pack_into('<H',packet,348,1250)
        return packet

    def test_registration_matches_native_protocol_layout(self):
        route,remote=relay_offer(self.offer(),'CLIENT','DEVICE')
        self.assertEqual((route,remote),(('192.0.2.3',1250),17))
        packet=relay_registration('CLIENT','DEVICE',1,remote)
        self.assertEqual(len(packet),212)
        self.assertEqual(struct.unpack_from('<HH',packet),(0x2012,3001))
        self.assertEqual(packet[4:104].rstrip(b'\0'),b'CLIENT')
        self.assertEqual(struct.unpack_from('<I',packet,104)[0],1)
        self.assertEqual(packet[108:208].rstrip(b'\0'),b'DEVICE')
        self.assertEqual(struct.unpack_from('<I',packet,208)[0],17)
        hello=peer_hello('CLIENT',route,remote,connection_type=3)
        self.assertEqual(struct.unpack_from('<IIII',hello,108),(17,127,1,3))

    def test_relay_offer_must_match_device_and_session(self):
        with self.assertRaises(ProtocolError):relay_offer(self.offer(),'OTHER','DEVICE')
        with self.assertRaises(ProtocolError):relay_offer(self.offer(),'CLIENT','OTHER')
        packet=self.offer();packet[332:348]=bytes(16);struct.pack_into('<H',packet,348,0)
        self.assertIsNone(relay_offer(packet,'CLIENT','DEVICE'))

    def test_multicast_and_truncated_offers_are_rejected(self):
        packet=self.offer();packet[332:348]=b'224.0.0.1'.ljust(16,b'\0')
        with self.assertRaises(ProtocolError):relay_offer(packet,'CLIENT','DEVICE')
        with self.assertRaises(ProtocolError):relay_offer(packet[:100],'CLIENT','DEVICE')
