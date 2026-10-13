"""Contrato HTTP exercitado com servidores locais, sem contas/equipamentos reais."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from simnext_linux.cloud import CloudClient, CloudConfig, CloudError, parse_cloud_device, local_devices, cloud_host


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.bad_next = False
        self.cycle = False
        self.refresh_invalid = False
        self.fail_first_list = False
        self.list_unauthorized = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, data, status=200):
                content = data if isinstance(data, bytes) else json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                form = parse_qs(raw.decode())
                owner.requests.append(("POST", self.path, dict(self.headers), form))
                if self.path == "/auth/token/":
                    refresh = form.get("grant_type") == ["refresh_token"]
                    if refresh and owner.refresh_invalid:
                        self.send({"error": "invalid_grant"}, 400)
                    else:
                        self.send({"access_token": "access-two" if refresh else "access-one",
                                   "refresh_token": "refresh-two" if refresh else "refresh-one",
                                   "expires_in": 3600})
                elif self.path == "/api/v1/auth/login/":
                    self.send(b"OK")
                elif self.path == "/api/v1/auth/check-user/":
                    self.send(b"OK")
                else:
                    self.send({}, 404)

            def do_GET(self):
                owner.requests.append(("GET", self.path, dict(self.headers), None))
                if self.path == "/api/v1/sim/capabilities/":
                    self.send({"simnext_api_max_page_size": 2})
                elif self.path.startswith(("/api/v1/sim/video-devices/", "/api/v1/isic/mobile/video-devices/")):
                    if owner.fail_first_list:
                        owner.fail_first_list = False
                        self.send({}, 401)
                        return
                    if owner.list_unauthorized:
                        self.send({}, 401)
                        return
                    second = "offset=2" in self.path
                    record = {"device_id": "second" if second else "first", "serial_number": "SERIAL-TEST",
                              "data": {"name": "Teste", "main_ip": "192.0.2.10", "model": "Modelo teste",
                                       "username": "operator", "password": "NEVER-PERSIST-THIS",
                                       "channels": [{"index": 0, "name": "Entrada"}, {"index": 1, "name": "Portão"}]}}
                    if self.path.startswith("/api/v1/isic/"):
                        record["id"] = 102 if second else 101
                        del record["device_id"]
                    next_url = None if second else "/api/v1/sim/video-devices/?limit=2&offset=2"
                    if owner.bad_next:
                        next_url = "https://untrusted.example/leak"
                    if owner.cycle:
                        next_url = "/api/v1/sim/video-devices/?limit=2"
                    if self.path.startswith("/api/v1/isic/") and next_url and not owner.bad_next:
                        next_url = next_url.replace("/api/v1/sim/", "/api/v1/isic/mobile/")
                    self.send({"Count": 2, "results": [record], "next": next_url})
                else:
                    self.send({}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        root = f"http://127.0.0.1:{self.server.server_port}"
        self.client = CloudClient(CloudConfig(
            authorize_url=root + "/auth/authorize", token_url=root + "/auth/token/",
            client_id="test-public-client", api_base=root, isic_api_base=root,
            devices_path="/api/v1/sim/video-devices/?limit={page_size}",
            capabilities_path="/api/v1/sim/capabilities/", scopes="openid profile",
            login_path="/api/v1/auth/login/", validate_path="/api/v1/auth/check-user/",
            installation_header="X-SIMNext-Client-ID", version_header="x-sim-app-version",
            refresh_installation_header="X-SIMNEXT-Client-ID"), installation_id="test-installation")

    def tearDown(self):
        self.client.logout()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def login(self, bad_state_first=False):
        attempt = self.client.create_login()
        query = parse_qs(urlsplit(attempt.authorization_url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        expected = base64.urlsafe_b64encode(hashlib.sha256(attempt.code_verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], [expected])
        self.assertEqual(query["redirect_uri"], [attempt.redirect_uri])
        self.assertNotIn("code_verifier", query)
        result = []
        waiter = threading.Thread(target=lambda: result.append(attempt.wait_for_code(threading.Event(), timeout=3)))
        waiter.start()
        if bad_state_first:
            for invalid in ("wrong", "%C3%A9"):
                with self.assertRaises(HTTPError) as error:
                    urlopen(attempt.redirect_uri + "?state=" + invalid + "&code=unexpected", timeout=2)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
        with urlopen(attempt.redirect_uri + "?state=" + attempt.state + "&code=local-test-code", timeout=2) as response:
            html = response.read().decode()
            self.assertNotIn("local-test-code", html)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        waiter.join(timeout=3)
        self.assertEqual(result, ["local-test-code"])
        self.client.finish_login(attempt, result[0])
        self.assertTrue(self.client.authenticated)
        self.assertEqual(attempt.code_verifier, "")
        return attempt

    def test_pkce_state_token_exchange_and_all_pages(self):
        attempt = self.login(bad_state_first=True)
        devices = self.client.list_devices()
        self.assertEqual([device.id for device in devices], ["first", "second"])
        self.assertFalse(hasattr(devices[0], "password"))
        imported = local_devices(devices[0])
        self.assertEqual([device.channel for device in imported], [1])
        self.assertEqual([device.id for device in imported], [device.id for device in local_devices(devices[0])])
        self.assertNotIn("password", vars(imported[0]))
        posts = [item for item in self.requests if item[1] == "/auth/token/"]
        self.assertEqual(posts[0][3]["grant_type"], ["authorization_code"])
        self.assertEqual(posts[0][3]["redirect_uri"], [attempt.redirect_uri])
        self.assertNotIn("client_secret", posts[0][3])
        pages = [item for item in self.requests if item[0] == "GET" and "video-devices" in item[1]]
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0][2]["Authorization"], "access-one")

    def test_isic_catalog_all_pages_without_simnext_capabilities_or_version(self):
        self.login()
        self.requests.clear()
        records = self.client.list_devices(source="isic")
        self.assertEqual([record.id for record in records], ["isic:101", "isic:102"])
        self.assertEqual(len(self.requests), 2)
        for method, path, headers, _ in self.requests:
            self.assertTrue(path.startswith("/api/v1/isic/mobile/video-devices/"))
            self.assertEqual(headers["Authorization"], "access-one")
            self.assertNotIn("x-sim-app-version", headers)

    def test_isic_rejects_other_origin_next(self):
        self.login()
        self.bad_next = True
        with self.assertRaisesRegex(CloudError, "destino não autorizado"):
            self.client.list_devices(source="isic")

    def test_device_credentials_are_session_only_and_cleared_on_logout(self):
        self.login()
        record = self.client.list_devices(source="isic")[0]
        device = local_devices(record)[0]
        self.assertEqual(self.client.device_password(device.id), "NEVER-PERSIST-THIS")
        self.assertNotIn("password", vars(device))
        self.client.logout()
        self.assertIsNone(self.client.device_password(device.id))

    def test_401_refreshes_once_and_uses_rotated_tokens(self):
        self.login()
        self.fail_first_list = True
        self.assertEqual(len(self.client.list_devices()), 2)
        refresh = [item for item in self.requests if item[3] and item[3].get("grant_type") == ["refresh_token"]]
        self.assertEqual(len(refresh), 1)
        self.assertEqual(refresh[0][3]["refresh_token"], ["refresh-one"])
        self.assertEqual(self.client._session.refresh_token, "refresh-two")
        self.assertEqual(self.requests[-1][2]["Authorization"], "access-two")

    def test_rejected_refresh_clears_session(self):
        self.login()
        self.fail_first_list = True
        self.refresh_invalid = True
        with self.assertRaises(CloudError):
            self.client.list_devices()
        self.assertFalse(self.client.authenticated)

    def test_other_origin_next_never_receives_token(self):
        self.login()
        self.bad_next = True
        with self.assertRaisesRegex(CloudError, "destino não autorizado"):
            self.client.list_devices()
        self.assertEqual(len([item for item in self.requests if "video-devices" in item[1]]), 1)

    def test_pagination_cycle_fails_without_partial_return(self):
        self.login()
        self.cycle = True
        with self.assertRaisesRegex(CloudError, "repetiu"):
            self.client.list_devices()

    def test_cloud_only_record_requires_local_address(self):
        record = parse_cloud_device({"device_id": "cloud-only", "serial_number": "TEST",
                                     "data": {"main_ip": "0.0.0.0", "channels": []}})
        self.assertIsNone(record.host)
        self.assertEqual(local_devices(record)[0].connection, "cloud")
        self.assertEqual(local_devices(record)[0].serial, "TEST")
        self.assertEqual(local_devices(record, "192.0.2.25")[0].host, "192.0.2.25")

    def test_isic_identifiers_accept_numeric_and_alternate_fields(self):
        for identifier_fields, expected in [({"device_id": 42}, "isic:42"),
                                            ({"id": 17}, "isic:17"),
                                            ({"id": "opaque-id"}, "isic:opaque-id"),
                                            ({"serial_number": "TEST-001"}, "isic:serial:TEST-001")]:
            with self.subTest(fields=identifier_fields):
                record = dict(identifier_fields, data={"name": "Teste", "main_ip": "192.0.2.10"})
                device = parse_cloud_device(record, source="isic")
                self.assertEqual(device.id, expected)
                self.assertEqual(local_devices(device)[0].id, local_devices(device)[0].id)
        nested = parse_cloud_device({"data": {"id": 33}}, source="isic")
        self.assertEqual(nested.id, "isic:33")

    def test_missing_or_invalid_identifiers_are_not_silently_merged(self):
        for value in (None, True, {}, [], "   ", -1):
            with self.subTest(value=value), self.assertRaisesRegex(CloudError, "sem identificador"):
                parse_cloud_device({"device_id": value, "data": {}}, source="isic")
        with self.assertRaisesRegex(CloudError, "sem identificador"):
            parse_cloud_device({"id": "isic-only", "data": {}})

    def test_schema_diagnostic_contains_types_without_field_values(self):
        self.login()
        schemas = []
        self.client.schema_callback = schemas.append
        self.client.list_devices(source="isic")
        self.assertEqual(schemas[0]["source"], "isic")
        self.assertEqual(schemas[0]["fields"]["id"], "int")
        self.assertEqual(schemas[0]["data_fields"]["password"], "str")
        encoded = json.dumps(schemas)
        for private_value in ("NEVER-PERSIST-THIS", "SERIAL-TEST", "192.0.2.10", "operator", "access-one"):
            self.assertNotIn(private_value, encoded)

    def test_ddns_urls_and_ports_are_read_without_changing_rtsp_port(self):
        for value in ("test.ddns-intelbras.com.br", "http://test.ddns-intelbras.com.br:8080/", "test.ddns-intelbras.com.br:80"):
            with self.subTest(value=value):
                record = parse_cloud_device({"id": 1, "serial_number": "TEST", "data": {
                    "ddns": value, "main_ip": "192.0.2.10", "service_port": 37777}}, source="isic")
                self.assertEqual(record.host, "test.ddns-intelbras.com.br")
                self.assertEqual(local_devices(record)[0].port, 554)
        for value in ("http://user:pass@test.example", "https://test.example/path", "test.example:0", "test.example?token=x"):
            self.assertIsNone(cloud_host(value))

    def test_cancelled_login_closes_port_without_token_request(self):
        attempt = self.client.create_login()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(CloudError, "cancelado"):
            attempt.wait_for_code(cancel)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.client.authenticated)


if __name__ == "__main__":
    unittest.main()

class PrivateConfigurationTests(unittest.TestCase):
    def test_public_defaults_do_not_include_application_identifier(self):
        from simnext_linux.cloud_config import CONFIG
        from simnext_linux.cloud import default_config, CloudError
        import os, tempfile
        from unittest.mock import patch
        self.assertEqual(CONFIG['client_id'], '')
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'XDG_CONFIG_HOME': folder}):
            with self.assertRaises(CloudError): default_config()

    def test_authorized_identifier_is_loaded_only_from_local_configuration(self):
        from simnext_linux.cloud import default_config
        import os, tempfile, json
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'XDG_CONFIG_HOME': folder}):
            directory = Path(folder) / 'simnext-linux'; directory.mkdir()
            (directory / 'cloud.json').write_text(json.dumps({'client_id': 'SYNTHETIC-CLIENT'}))
            self.assertEqual(default_config().client_id, 'SYNTHETIC-CLIENT')
