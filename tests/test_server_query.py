from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from launcher import build_samp_query_packet, format_client_exit_code, query_samp_server


class FakeUdpSocket:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []
        self.timeout = None
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendto(self, packet, address):
        self.sent.append((packet, address))

    def recvfrom(self, _size):
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        self.closed = True


class SampServerQueryTests(unittest.TestCase):
    def test_builds_sa_mp_ping_packet_with_little_endian_port(self):
        packet = build_samp_query_packet(
            "188.127.241.8",
            1130,
            opcode="p",
            challenge=b"abcd",
        )

        self.assertEqual(
            packet,
            b"SAMP" + bytes((188, 127, 241, 8)) + b"\x6a\x04" + b"pabcd",
        )

    def test_reports_online_and_measured_ping_when_server_echoes_challenge(self):
        address = ("188.127.241.8", 1130)
        query = build_samp_query_packet(
            address[0], address[1], opcode="p", challenge=b"wxyz"
        )
        udp = FakeUdpSocket([(query[:11] + b"wxyz", address)])

        with (
            patch("launcher.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, "", address)
            ]),
            patch("launcher.socket.socket", return_value=udp),
        ):
            result = query_samp_server("188.127.241.8", 1130, timeout=0.05, challenge=b"wxyz")

        self.assertTrue(result["online"])
        self.assertIsInstance(result["ping_ms"], int)
        self.assertGreaterEqual(result["ping_ms"], 0)
        self.assertEqual(result["method"], "ping")
        self.assertTrue(udp.closed)

    def test_falls_back_to_valid_info_response_if_ping_is_not_supported(self):
        address = ("188.127.241.8", 1130)
        info = build_samp_query_packet(address[0], address[1], opcode="i")
        udp = FakeUdpSocket([socket.timeout(), (info + b"\x00", address)])

        with (
            patch("launcher.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, "", address)
            ]),
            patch("launcher.socket.socket", return_value=udp),
        ):
            result = query_samp_server("188.127.241.8", 1130, timeout=0.05, challenge=b"wxyz")

        self.assertTrue(result["online"])
        self.assertEqual(result["method"], "info")
        self.assertIsInstance(result["ping_ms"], int)
        self.assertEqual(len(udp.sent), 2)

    def test_reports_offline_after_both_query_packets_time_out(self):
        address = ("188.127.241.8", 1130)
        udp = FakeUdpSocket([socket.timeout(), socket.timeout()])

        with (
            patch("launcher.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, "", address)
            ]),
            patch("launcher.socket.socket", return_value=udp),
        ):
            result = query_samp_server("188.127.241.8", 1130, timeout=0.01, challenge=b"wxyz")

        self.assertFalse(result["online"])
        self.assertIsNone(result["ping_ms"])
        self.assertTrue(udp.closed)

    def test_explains_windows_access_violation_exit_code(self):
        message = format_client_exit_code(3221225477)

        self.assertIn("0xC0000005", message)
        self.assertIn("ACCESS_VIOLATION", message)
        self.assertIn("Переустановить", message)

    def test_rejects_a_response_with_a_different_ping_challenge(self):
        address = ("188.127.241.8", 1130)
        query = build_samp_query_packet(
            address[0], address[1], opcode="p", challenge=b"wxyz"
        )
        info = build_samp_query_packet(address[0], address[1], opcode="i")
        udp = FakeUdpSocket([(query[:11] + b"NOPE", address), socket.timeout()])

        with (
            patch("launcher.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, "", address)
            ]),
            patch("launcher.socket.socket", return_value=udp),
        ):
            result = query_samp_server("188.127.241.8", 1130, timeout=0.01, challenge=b"wxyz")

        self.assertFalse(result["online"])
        self.assertIsNone(result["ping_ms"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
