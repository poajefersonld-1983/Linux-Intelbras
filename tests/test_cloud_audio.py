import io
import os
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication
from simnext_linux.cloud_audio import CloudAudioWorker, MAX_BUFFER
from simnext_linux.domain import Device
from simnext_linux.gui import VideoTile


class CloudAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_pcm_is_bounded_and_notifications_are_coalesced(self):
        worker = CloudAudioWorker('unused')
        notices = []
        worker.pcm_ready.connect(lambda: notices.append(True))
        worker.publish_pcm(struct.pack('<h', 1000) * MAX_BUFFER)
        worker.publish_pcm(struct.pack('<h', 2000) * MAX_BUFFER)
        self.assertEqual(notices, [True])
        self.assertEqual(len(worker.take_pcm()), MAX_BUFFER)
        self.assertEqual(worker.take_pcm(), b'')
        self.assertAlmostEqual(worker.level_dbfs, -24.29, places=1)

    def test_only_audio_is_requested_without_exposing_credentials(self):
        commands, playlists = [], []
        class Process:
            def __init__(self, command, **kwargs):
                commands.append(command)
                playlists.append(os.read(kwargs['pass_fds'][0], 4096))
                self.stdout = io.BytesIO(struct.pack('<h', 400) * 1600)
            def poll(self): return 0
            def wait(self, timeout): return 0
        worker = CloudAudioWorker('rtsp://operator:PRIVATE@127.0.0.1/live')
        with patch('simnext_linux.cloud_audio.subprocess.Popen', Process):
            worker.run()
        self.assertEqual(worker.received_bytes, 3200)
        self.assertIn(b'option allowed_media_types audio', playlists[0])
        self.assertNotIn('PRIVATE', str(commands))
        self.assertEqual(worker.source, '')

    def test_cancel_before_start_does_not_connect(self):
        worker = CloudAudioWorker('rtsp://operator:PRIVATE@127.0.0.1/live')
        worker.cancel()
        with patch('simnext_linux.cloud_audio.subprocess.Popen') as popen:
            worker.run()
        popen.assert_not_called()
        self.assertEqual(worker.source, '')

    def test_toggle_off_cancels_audio_without_disconnecting_video(self):
        tile = VideoTile(1)
        tile.device_id = 'test-camera'
        tile._active_device = Device('test-camera', 'Synthetic', '', connection='cloud', serial='SYNTHETIC')
        marker = object()
        tile.last_image = marker
        worker = SimpleNamespace(cancelled=False)
        worker.cancel = lambda: setattr(worker, 'cancelled', True)
        tile.cloud_audio_worker = worker
        tile.mute.setChecked(True)  # No live bridge: no network connection.
        # start_cloud_audio stopped the previous worker before checking the bridge.
        self.assertTrue(worker.cancelled)
        tile.mute.setChecked(False)
        self.assertEqual(tile.device_id, 'test-camera')
        self.assertIs(tile.last_image, marker)
        tile.disconnect()

    def test_audio_failure_leaves_video_connected(self):
        tile = VideoTile(1)
        tile.device_id = 'test-camera'
        tile._active_device = Device('test-camera', 'Synthetic', '', connection='cloud', serial='SYNTHETIC')
        tile.status.setText('Reproduzindo via Intelbras Cloud')
        tile.cloud_audio_failed('Sem áudio neste canal.', tile._audio_generation)
        self.assertFalse(tile.mute.isChecked())
        self.assertEqual(tile.device_id, 'test-camera')
        self.assertEqual(tile.status.text(), 'Reproduzindo via Intelbras Cloud')
        self.assertEqual(tile.mute.toolTip(), 'Sem áudio neste canal.')
        tile.disconnect()

    def test_old_audio_callbacks_cannot_feed_a_new_connection(self):
        tile = VideoTile(1)
        worker = SimpleNamespace(take_pcm=lambda: self.fail('Old audio was consumed'))
        tile.receive_cloud_audio(worker, tile._audio_generation - 1)
        self.assertEqual(tile._audio_pending, b'')
        tile.disconnect()

    def test_enabled_cloud_audio_reaches_sink_and_partial_writes_are_retained(self):
        class Sink:
            def __init__(self, *args): self.data = bytearray(); self.stopped = False
            def setBufferSize(self, size): pass
            def setVolume(self, volume): pass
            def start(self): return self
            def bytesFree(self): return 1024
            def write(self, data):
                count = min(200, len(data)); self.data.extend(data[:count]); return count
            def stop(self): self.stopped = True
            def deleteLater(self): pass
        output = SimpleNamespace(isNull=lambda: False, isFormatSupported=lambda fmt: True)
        tile = VideoTile(1)
        tile.device_id = 'test-camera'
        tile._active_device = Device('test-camera', 'Synthetic', '', connection='cloud', serial='SYNTHETIC')
        tile._session_password = 'PRIVATE'
        tile.p2p_bridge = SimpleNamespace(entry={'bridge': SimpleNamespace(proxy=SimpleNamespace(port=12345))}, close=lambda: None)
        with patch('simnext_linux.gui.QMediaDevices.defaultAudioOutput', return_value=output), \
             patch('simnext_linux.gui.QAudioSink', Sink), \
             patch.object(CloudAudioWorker, 'start'):
            tile.mute.setChecked(True)
            worker, sink = tile.cloud_audio_worker, tile._audio_sink
            self.assertIsNotNone(worker)
            pcm = struct.pack('<h', 1234) * 320
            worker.publish_pcm(pcm)
            self.assertEqual(len(tile._audio_pending), 440)
            for _ in range(4): tile.flush_cloud_audio()
            self.assertEqual(sink.data, pcm)
            self.assertEqual(tile._audio_pending, b'')
            tile.mute.setChecked(False)
            self.assertTrue(worker.cancelled.is_set())
            self.assertTrue(sink.stopped)
            self.assertEqual(tile.device_id, 'test-camera')
        tile.disconnect()
