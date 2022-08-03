# Part of Odoo. See LICENSE file for full copyright and licensing details.

import gc
import json
from collections import defaultdict
from datetime import timedelta
from freezegun import freeze_time
from unittest.mock import patch

from odoo.api import Environment
from odoo.tests import common, new_test_user
from .common import WebsocketCase
from ..models.bus import dispatch
from ..websocket import (
    CloseCode,
    Frame,
    Opcode,
    TimeoutManager,
    TimeoutReason,
    Websocket
)

@common.tagged('post_install', '-at_install')
class TestWebsocketCaryall(WebsocketCase):
    def test_lifecycle_hooks(self):
        events = []
        with patch.object(Websocket, '_event_callbacks', defaultdict(set)):
            @Websocket.onopen
            def onopen(env, websocket):  # pylint: disable=unused-variable
                self.assertIsInstance(env, Environment)
                self.assertIsInstance(websocket, Websocket)
                events.append('open')

            @Websocket.onclose
            def onclose(env, websocket):  # pylint: disable=unused-variable
                self.assertIsInstance(env, Environment)
                self.assertIsInstance(websocket, Websocket)
                events.append('close')

            ws = self.websocket_connect()
            ws.close(CloseCode.CLEAN)
            self.wait_remaining_websocket_connections()
            self.assertEqual(events, ['open', 'close'])

    def test_instances_weak_set(self):
        gc.collect()
        first_ws = self.websocket_connect()
        second_ws = self.websocket_connect()
        self.assertEqual(len(Websocket._instances), 2)
        first_ws.close(CloseCode.CLEAN)
        second_ws.close(CloseCode.CLEAN)
        self.wait_remaining_websocket_connections()
        # serve_forever_patch prevent websocket instances from being
        # collected. Stop it now.
        self._serve_forever_patch.stop()
        gc.collect()
        self.assertEqual(len(Websocket._instances), 0)

    def test_timeout_manager_no_response_timeout(self):
        with freeze_time('2022-08-19') as frozen_time:
            timeout_manager = TimeoutManager()
            # A PING frame was just sent, if no pong has been received
            # within TIMEOUT seconds, the connection should have timed out.
            timeout_manager.acknowledge_frame_sent(Frame(Opcode.PING))
            self.assertEqual(timeout_manager._awaited_opcode, Opcode.PONG)
            frozen_time.tick(delta=timedelta(seconds=TimeoutManager.TIMEOUT / 2))
            self.assertFalse(timeout_manager.has_timed_out())
            frozen_time.tick(delta=timedelta(seconds=TimeoutManager.TIMEOUT / 2))
            self.assertTrue(timeout_manager.has_timed_out())
            self.assertEqual(timeout_manager.timeout_reason, TimeoutReason.NO_RESPONSE)

            timeout_manager = TimeoutManager()
            # A CLOSE frame was just sent, if no close has been received
            # within TIMEOUT seconds, the connection should have timed out.
            timeout_manager.acknowledge_frame_sent(Frame(Opcode.CLOSE))
            self.assertEqual(timeout_manager._awaited_opcode, Opcode.CLOSE)
            frozen_time.tick(delta=timedelta(seconds=TimeoutManager.TIMEOUT / 2))
            self.assertFalse(timeout_manager.has_timed_out())
            frozen_time.tick(delta=timedelta(seconds=TimeoutManager.TIMEOUT / 2))
            self.assertTrue(timeout_manager.has_timed_out())
            self.assertEqual(timeout_manager.timeout_reason, TimeoutReason.NO_RESPONSE)

    def test_timeout_manager_keep_alive_timeout(self):
        with freeze_time('2022-08-19') as frozen_time:
            timeout_manager = TimeoutManager()
            frozen_time.tick(delta=timedelta(seconds=TimeoutManager.KEEP_ALIVE_TIMEOUT / 2))
            self.assertFalse(timeout_manager.has_timed_out())
            frozen_time.tick(delta=timedelta(seconds=TimeoutManager.KEEP_ALIVE_TIMEOUT / 2))
            self.assertTrue(timeout_manager.has_timed_out())
            self.assertEqual(timeout_manager.timeout_reason, TimeoutReason.KEEP_ALIVE)

    def test_timeout_manager_reset_wait_for(self):
        timeout_manager = TimeoutManager()
        # PING frame
        timeout_manager.acknowledge_frame_sent(Frame(Opcode.PING))
        self.assertEqual(timeout_manager._awaited_opcode, Opcode.PONG)
        timeout_manager.acknowledge_frame_receipt(Frame(Opcode.PONG))
        self.assertIsNone(timeout_manager._awaited_opcode)

        # CLOSE frame
        timeout_manager.acknowledge_frame_sent(Frame(Opcode.CLOSE))
        self.assertEqual(timeout_manager._awaited_opcode, Opcode.CLOSE)
        timeout_manager.acknowledge_frame_receipt(Frame(Opcode.CLOSE))
        self.assertIsNone(timeout_manager._awaited_opcode)

    def test_user_login(self):
        websocket = self.websocket_connect()
        new_test_user(self.env, login='test_user', password='Password!1')
        self.authenticate('test_user', 'Password!1')
        # The session with whom the websocket connected has been
        # deleted. WebSocket should disconnect in order for the
        # session to be updated.
        websocket.send(json.dumps({'event_name': 'subscribe'}))
        self.assert_close_with_code(websocket, CloseCode.SESSION_EXPIRED)

    def test_user_logout_incoming_message(self):
        new_test_user(self.env, login='test_user', password='Password!1')
        user_session = self.authenticate('test_user', 'Password!1')
        websocket = self.websocket_connect(cookie=f'session_id={user_session.sid};')
        self.url_open('/web/session/logout')
        # The session with whom the websocket connected has been
        # deleted. WebSocket should disconnect in order for the
        # session to be updated.
        websocket.send(json.dumps({'event_name': 'subscribe'}))
        self.assert_close_with_code(websocket, CloseCode.SESSION_EXPIRED)

    def test_user_logout_outgoing_message(self):
        new_test_user(self.env, login='test_user', password='Password!1')
        user_session = self.authenticate('test_user', 'Password!1')
        websocket = self.websocket_connect(cookie=f'session_id={user_session.sid};')
        with patch.object(dispatch, '_ws_to_subscription', {}):
            websocket.send(json.dumps({
                'event_name': 'subscribe',
                'data': {'channels': ['channel1'], 'last': 0}
            }))
            self.url_open('/web/session/logout')
            # Simulate postgres notify. The session with whom the websocket
            # connected has been deleted. WebSocket should be closed without
            # receiving the message.
            self.env['bus.bus']._sendone('channel1', 'notif type', 'message')
            dispatch._dispatch_notifications(next(iter(dispatch._ws_to_subscription.keys())))
            self.assert_close_with_code(websocket, CloseCode.SESSION_EXPIRED)
