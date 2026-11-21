import logging
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import app as app_module


class LogRotationTests(unittest.TestCase):
    def test_app_log_rotates_instead_of_growing_forever(self):
        log_dir = tempfile.mkdtemp()
        configured = {}
        with patch.object(app_module.Config, 'LOG_DIR', log_dir, create=True), \
                patch.object(app_module, 'LOG_MAX_BYTES', 2000), \
                patch.object(app_module.logging, 'basicConfig', side_effect=lambda **kwargs: configured.update(kwargs)), \
                patch.object(app_module.signal, 'signal'), \
                patch.object(app_module.scraper, 'teardown_selenium'), \
                patch.object(app_module, 'create_app', return_value=MagicMock()):
            app_module.main()
        file_handler = next(handler for handler in configured['handlers'] if isinstance(handler, logging.FileHandler))
        self.addCleanup(file_handler.close)
        self.assertEqual(file_handler.baseFilename, os.path.join(log_dir, 'app.log'))
        self.assertEqual(file_handler.backupCount, app_module.LOG_BACKUP_COUNT)
        for number in range(400):
            file_handler.emit(logging.makeLogRecord({'msg': f'line {number} ' + 'x' * 40, 'levelno': logging.INFO}))
        files = sorted(os.listdir(log_dir))
        self.assertEqual(len(files), app_module.LOG_BACKUP_COUNT + 1)  # app.log plus the kept backups
        self.assertTrue(all(os.path.getsize(os.path.join(log_dir, name)) <= 2000 for name in files))


if __name__ == '__main__':
    unittest.main()
