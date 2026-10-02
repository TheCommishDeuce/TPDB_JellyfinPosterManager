import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError


spec = importlib.util.spec_from_file_location('container_healthcheck', Path(__file__).resolve().parents[1] / 'container-healthcheck.py')
healthcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(healthcheck)


class ContainerHealthcheckTests(unittest.TestCase):
    def test_uses_custom_port_and_requires_liveness_response(self):
        response = MagicMock(wraps=io.BytesIO(b'{"status":"alive"}'))
        response.status = 200
        with patch.dict(os.environ, {'WEB_PORT': '5087'}), patch.object(healthcheck, 'urlopen') as request:
            request.return_value.__enter__.return_value = response
            healthcheck.check()
        request.assert_called_once_with('http://127.0.0.1:5087/health/live', timeout=3)

    def test_rejects_wrong_status_payload(self):
        response = MagicMock(wraps=io.BytesIO(b'{"status":"degraded"}'))
        response.status = 200
        with patch.object(healthcheck, 'urlopen') as request:
            request.return_value.__enter__.return_value = response
            with self.assertRaisesRegex(ValueError, 'Unexpected liveness'):
                healthcheck.check()

    def test_unreachable_app_fails_health_check(self):
        with patch.object(healthcheck, 'urlopen', side_effect=URLError('connection refused')):
            with self.assertRaises(URLError):
                healthcheck.check()


if __name__ == '__main__':
    unittest.main()
