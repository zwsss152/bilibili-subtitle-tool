"""Construct the real Tk window against an isolated library, without network."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.paths import PROJECT_ROOT
from src.core import documents, library, rename
from src.services import reading_service
from src.services.scheduler import Scheduler
from src.ui import batch_app


class CloudUISmokeTests(unittest.TestCase):
    def test_cloud_window_builds_saves_settings_and_closes(self):
        artifacts = PROJECT_ROOT / 'tests' / '.artifacts'
        artifacts.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifacts) as temporary, ExitStack() as stack:
            root = Path(temporary)
            for module in (batch_app, documents, library, rename, reading_service):
                stack.enter_context(patch.object(module, 'OUTPUT_ROOT', root / 'output'))
            stack.enter_context(patch.object(batch_app, 'load_config', return_value={'engine': 'cloud'}))
            saved = stack.enter_context(patch.object(batch_app, 'save_config'))
            stack.enter_context(patch.object(batch_app, 'Scheduler', side_effect=lambda **kw:
                Scheduler(state_path=root / 'queue.json', autostart=False)))
            app = batch_app.BatchApp(autostart=False)
            app.withdraw()
            try:
                app.update_idletasks()
                self.assertFalse(hasattr(app, 'engine_box'))
                self.assertFalse(hasattr(app, 'download_punctuation'))
                self.assertFalse(hasattr(app.scheduler, 'editor'))
                app._save_settings()
                self.assertEqual(saved.call_args.kwargs['engine'], 'cloud')
            finally:
                app.close()
            self.assertIsNone(app.scheduler.process)
