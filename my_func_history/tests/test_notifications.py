import base64
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests

import notifications
from common import decode_queue_body


def response(status_code=200, body=None):
    mock = MagicMock(status_code=status_code)
    mock.json.return_value = body or {}
    if status_code >= 400:
        mock.raise_for_status.side_effect = requests.HTTPError(str(status_code))
    return mock


def fake_db(tokens):
    db = MagicMock()
    collections = {"push_tokens": MagicMock(), "push_tickets": MagicMock()}
    db.__getitem__.side_effect = collections.__getitem__
    collections["push_tokens"].find.return_value = [{"token": t} for t in tokens]
    return db, collections


EVENT = {"type": "custom", "recipients": "all", "title": "Oi", "body": "Teste", "data": {"x": 1}}


class DecodeTestCase(unittest.TestCase):
    def test_accepts_plain_and_base64_json(self):
        raw = json.dumps(EVENT).encode("utf-8")
        self.assertEqual(decode_queue_body(raw), EVENT)
        self.assertEqual(decode_queue_body(base64.b64encode(raw)), EVENT)


@patch("notifications.time.sleep")
@patch("notifications.requests.post")
class SendNotificationTestCase(unittest.TestCase):
    def test_sends_to_all_tokens_and_stores_tickets(self, post, sleep):
        db, collections = fake_db(["t1", "t2"])
        post.return_value = response(body={"data": [
            {"status": "ok", "id": "r1"},
            {"status": "ok", "id": "r2"},
        ]})

        enviados = notifications.send_notification(db, EVENT)

        self.assertEqual(enviados, 2)
        collections["push_tokens"].find.assert_called_once_with({}, {"_id": 0, "token": 1})
        messages = post.call_args.kwargs["json"]
        self.assertEqual([m["to"] for m in messages], ["t1", "t2"])
        self.assertEqual(messages[0]["data"], {"x": 1, "type": "custom"})
        stored = collections["push_tickets"].insert_many.call_args.args[0]
        self.assertEqual([t["ticket_id"] for t in stored], ["r1", "r2"])

    def test_filters_by_recipients(self, post, sleep):
        db, collections = fake_db([])

        enviados = notifications.send_notification(db, {**EVENT, "recipients": ["ana"]})

        self.assertEqual(enviados, 0)
        collections["push_tokens"].find.assert_called_once_with(
            {"user_id": {"$in": ["ana"]}}, {"_id": 0, "token": 1}
        )
        post.assert_not_called()

    def test_splits_in_batches_of_100(self, post, sleep):
        tokens = [f"t{i}" for i in range(150)]
        db, _ = fake_db(tokens)
        post.side_effect = lambda url, json, **kw: response(
            body={"data": [{"status": "ok", "id": m["to"]} for m in json]}
        )

        self.assertEqual(notifications.send_notification(db, EVENT), 150)
        self.assertEqual([len(c.kwargs["json"]) for c in post.call_args_list], [100, 50])

    def test_removes_unregistered_devices(self, post, sleep):
        db, collections = fake_db(["t1", "t2"])
        post.return_value = response(body={"data": [
            {"status": "error", "message": "x", "details": {"error": "DeviceNotRegistered"}},
            {"status": "ok", "id": "r2"},
        ]})

        notifications.send_notification(db, EVENT)

        collections["push_tokens"].delete_many.assert_called_once_with({"token": {"$in": ["t1"]}})

    def test_retries_temporary_errors(self, post, sleep):
        db, _ = fake_db(["t1"])
        post.side_effect = [
            requests.ConnectionError(),
            response(503),
            response(body={"data": [{"status": "ok", "id": "r1"}]}),
        ]

        self.assertEqual(notifications.send_notification(db, EVENT), 1)
        self.assertEqual(post.call_count, 3)

    def test_gives_up_after_retries(self, post, sleep):
        db, _ = fake_db(["t1"])
        post.return_value = response(429)

        with self.assertRaises(notifications.ExpoTemporaryError):
            notifications.send_notification(db, EVENT)
        self.assertEqual(post.call_count, notifications.TENTATIVAS_HTTP)

    def test_does_not_retry_bad_request(self, post, sleep):
        db, _ = fake_db(["t1"])
        post.return_value = response(400)

        with self.assertRaises(requests.HTTPError):
            notifications.send_notification(db, EVENT)
        self.assertEqual(post.call_count, 1)

    def test_rejects_invalid_event(self, post, sleep):
        db, _ = fake_db(["t1"])
        for invalido in ({**EVENT, "title": ""}, {**EVENT, "recipients": []}, {**EVENT, "data": "x"}):
            with self.assertRaises(ValueError):
                notifications.send_notification(db, invalido)
        post.assert_not_called()


@patch("notifications.requests.post")
class CheckReceiptsTestCase(unittest.TestCase):
    def test_removes_tokens_from_failed_receipts(self, post):
        db, collections = fake_db([])
        collections["push_tickets"].find.return_value.limit.return_value = [
            {"ticket_id": "r1", "token": "t1"},
            {"ticket_id": "r2", "token": "t2"},
            {"ticket_id": "r3", "token": "t3"},
        ]
        post.return_value = response(body={"data": {
            "r1": {"status": "ok"},
            "r2": {"status": "error", "details": {"error": "DeviceNotRegistered"}},
            "r3": {"status": "error", "details": {"error": "MessageTooBig"}},
        }})
        now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)

        verificados = notifications.check_receipts(db, now)

        self.assertEqual(verificados, 3)
        filtro = collections["push_tickets"].find.call_args.args[0]
        self.assertEqual(filtro, {"created_at": {"$lte": now - timedelta(minutes=15)}})
        self.assertEqual(post.call_args.kwargs["json"], {"ids": ["r1", "r2", "r3"]})
        collections["push_tokens"].delete_many.assert_called_once_with({"token": {"$in": ["t2"]}})
        collections["push_tickets"].delete_many.assert_called_once_with(
            {"ticket_id": {"$in": ["r1", "r2", "r3"]}}
        )


@patch("notifications.send_notification", return_value=1)
class DailyReminderTestCase(unittest.TestCase):
    def db(self, with_device, active_today):
        db, collections = fake_db([])
        collections["user_activity"] = MagicMock()
        collections["push_tokens"].distinct.return_value = with_device
        collections["user_activity"].distinct.return_value = active_today
        return db, collections

    def test_reminds_only_users_inactive_today(self, send):
        db, collections = self.db(["ana", "bruno", "carla"], ["bruno"])
        now = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)

        notifications.send_daily_reminder(db, now)

        collections["user_activity"].distinct.assert_called_once_with(
            "user_id", {"last_active_date": "2026-11-04"}
        )
        payload = send.call_args.args[1]
        self.assertEqual(payload["type"], "daily_reminder")
        self.assertEqual(payload["recipients"], ["ana", "carla"])

    def test_today_uses_local_timezone(self, send):
        db, collections = self.db(["ana"], [])
        # 02:00 UTC do dia 5 ainda é dia 4 em São Paulo
        notifications.send_daily_reminder(db, datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc))

        filtro = collections["user_activity"].distinct.call_args.args[1]
        self.assertEqual(filtro, {"last_active_date": "2026-11-04"})

    def test_skips_when_everyone_was_active(self, send):
        db, _ = self.db(["ana"], ["ana"])

        self.assertEqual(notifications.send_daily_reminder(db), 0)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
