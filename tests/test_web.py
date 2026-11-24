"""端到端测试：Flask 路由 + SQLite 事务语义。"""
import threading

import pytest

from app import create_app
from app import services
from app.db import get_db

SNAP = "2026-11-20T00:00:00Z"
CHECK = "2026-11-20T06:00:00Z"


@pytest.fixture()
def app(tmp_path):
    app = create_app({"TESTING": True, "AUDIT_DB": str(tmp_path / "test.db")})
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def _setup_round(client):
    resp = client.post("/api/round", json={"snapshot_at": SNAP, "check_at": CHECK})
    assert resp.status_code == 201, resp.data


def _add_terminal(client, name, expiry=None):
    resp = client.post("/api/terminals", json={"name": name, "snapshot_expiry": expiry})
    assert resp.status_code == 201, resp.data


def _pio(client, rows):
    return client.post("/api/pio-batch", json=rows)


# ---------------------------------------------------------------- 基础流程

def test_round_requires_timezone(client):
    resp = client.post("/api/round", json={"snapshot_at": "2026-11-20T00:00:00", "check_at": CHECK})
    assert resp.status_code == 400
    assert "时区" in resp.get_json()["error"]


def test_round_check_must_be_after_snapshot(client):
    resp = client.post("/api/round",
                       json={"snapshot_at": CHECK, "check_at": SNAP})
    assert resp.status_code == 400


def test_full_flow_and_results(client):
    _setup_round(client)
    _add_terminal(client, "PC-A", "2026-11-20T04:00:00Z")
    _add_terminal(client, "PC-B", "2026-11-20T02:00:00Z")
    _add_terminal(client, "PC-C", None)

    # 乱序提交
    resp = _pio(client, [
        {"terminal": "PC-A", "seq": 1, "router": "R2",
         "received_at": "2026-11-20T02:00:00Z", "valid_lifetime": 300},   # 剩余 ≤2h → e2
        {"terminal": "PC-A", "seq": 0, "router": "R1",
         "received_at": "2026-11-20T01:00:00Z", "valid_lifetime": 3600},  # 剩余 3h → e3
    ])
    assert resp.status_code == 200, resp.data
    assert resp.get_json()["inserted"] == 2

    data = client.get("/api/results").get_json()
    by_name = {t["name"]: t for t in data["terminals"]}

    # PC-A：e3 截断到 01:00+2h=03:00，核查 06:00 已过期
    pca = by_name["PC-A"]
    assert pca["expiry_at"] == "2026-11-20T03:00:00Z"
    assert pca["alive_at_check"] is False
    assert pca["last_change"]["seq"] == 0  # seq1 被 e2 忽略，最后改变者仍是 seq0

    # PC-B 无通告：结论保持快照，02:00 < 核查 06:00，未持有
    pcb = by_name["PC-B"]
    assert pcb["source"] == "snapshot"
    assert pcb["last_change"] is None
    assert pcb["alive_at_check"] is False

    # PC-C 快照无地址且无通告
    pcc = by_name["PC-C"]
    assert pcc["expiry_at"] == ""
    assert pcc["alive_at_check"] is False


def test_alive_terminal_reports_expiry_and_last_change(client):
    _setup_round(client)
    _add_terminal(client, "PC-D", None)
    resp = _pio(client, [
        {"terminal": "PC-D", "seq": 0, "router": "R1",
         "received_at": "2026-11-20T03:00:00Z", "valid_lifetime": 86400},
    ])
    assert resp.status_code == 200
    data = client.get("/api/results").get_json()
    pcd = data["terminals"][0]
    assert pcd["alive_at_check"] is True
    assert pcd["expiry_at"] == "2026-11-21T03:00:00Z"
    assert pcd["last_change"] == {
        "router": "R1", "seq": 0,
        "received_at": "2026-11-20T03:00:00Z", "valid_lifetime": 86400,
    }


def test_boundary_expiry_equal_to_check_time_is_not_alive(client):
    _setup_round(client)
    _add_terminal(client, "PC-E", None)
    resp = _pio(client, [
        {"terminal": "PC-E", "seq": 0, "router": "R1",
         "received_at": "2026-11-20T02:00:00Z", "valid_lifetime": 14400},  # 06:00 恰好到期
    ])
    assert resp.status_code == 200
    pce = client.get("/api/results").get_json()["terminals"][0]
    assert pce["expiry_at"] == "2026-11-20T06:00:00Z"
    assert pce["alive_at_check"] is False


# ---------------------------------------------------------------- 重报 / 冲突

def test_identical_resubmit_is_idempotent_and_not_recomputed(client):
    _setup_round(client)
    _add_terminal(client, "PC-A", "2026-11-20T04:00:00Z")
    row = {"terminal": "PC-A", "seq": 0, "router": "R1",
           "received_at": "2026-11-20T01:00:00Z", "valid_lifetime": 3600}
    first = _pio(client, [row]).get_json()
    assert first == {"inserted": 1, "duplicates_skipped": 0, "recomputed": ["PC-A"]}

    second = _pio(client, [row]).get_json()
    assert second == {"inserted": 0, "duplicates_skipped": 1, "recomputed": []}

    # 库里仍只有一条记录
    with get_db(client.application.config["AUDIT_DB"]) as conn:
        count = conn.execute("SELECT COUNT(*) c FROM pio_records").fetchone()["c"]
    assert count == 1


def test_same_seq_different_content_rejects_whole_batch_and_keeps_old_result(client):
    _setup_round(client)
    _add_terminal(client, "PC-A", "2026-11-20T04:00:00Z")
    _add_terminal(client, "PC-B", "2026-11-20T04:00:00Z")
    ok_row = {"terminal": "PC-A", "seq": 0, "router": "R1",
              "received_at": "2026-11-20T01:00:00Z", "valid_lifetime": 3600}
    assert _pio(client, [ok_row]).status_code == 200
    before = client.get("/api/results").get_json()

    # 同号异内容 + 另一个终端的新记录混在一批 → 整批退回
    bad_batch = [
        {"terminal": "PC-A", "seq": 0, "router": "R9",
         "received_at": "2026-11-20T01:00:00Z", "valid_lifetime": 3600},
        {"terminal": "PC-B", "seq": 0, "router": "R1",
         "received_at": "2026-11-20T01:30:00Z", "valid_lifetime": 100},
    ]
    resp = _pio(client, bad_batch)
    assert resp.status_code == 409
    assert resp.get_json()["row_errors"]

    after = client.get("/api/results").get_json()
    assert before == after  # 旧结果原样可查
    with get_db(client.application.config["AUDIT_DB"]) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM pio_records").fetchone()["c"] == 1


def test_validation_errors_reject_whole_batch(client):
    _setup_round(client)
    _add_terminal(client, "PC-A", "2026-11-20T04:00:00Z")
    resp = _pio(client, [
        {"terminal": "PC-A", "seq": 0, "router": "R1",
         "received_at": "2026-11-20T01:00:00Z", "valid_lifetime": 3600},
        {"terminal": "PC-A", "seq": 1, "router": "R1",
         "received_at": "2026-11-19T23:00:00Z", "valid_lifetime": 3600},  # 早于快照
        {"terminal": "GHOST", "seq": 0, "router": "R1",
         "received_at": "2026-11-20T01:00:00Z", "valid_lifetime": 3600},  # 未知终端
    ])
    assert resp.status_code == 409
    errors = resp.get_json()["row_errors"]
    rows = {e["row"] for e in errors}
    assert rows == {2, 3}  # 窗口错误 + 未知终端错误
    with get_db(client.application.config["AUDIT_DB"]) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM pio_records").fetchone()["c"] == 0


def test_in_batch_duplicate_seq_rejected(client):
    _setup_round(client)
    _add_terminal(client, "PC-A", "2026-11-20T04:00:00Z")
    row = {"terminal": "PC-A", "seq": 0, "router": "R1",
           "received_at": "2026-11-20T01:00:00Z", "valid_lifetime": 3600}
    resp = _pio(client, [row, dict(row, router="R2")])
    assert resp.status_code == 409


# ---------------------------------------------------------------- 增量重算

def test_only_affected_terminal_recomputed(client):
    _setup_round(client)
    _add_terminal(client, "PC-A", "2026-11-20T04:00:00Z")
    _add_terminal(client, "PC-B", "2026-11-20T04:00:00Z")
    _pio(client, [
        {"terminal": "PC-A", "seq": 0, "router": "R1",
         "received_at": "2026-11-20T01:00:00Z", "valid_lifetime": 3600},
        {"terminal": "PC-B", "seq": 0, "router": "R1",
         "received_at": "2026-11-20T01:00:00Z", "valid_lifetime": 3600},
    ])
    db_path = client.application.config["AUDIT_DB"]
    with get_db(db_path) as conn:
        first = {r["name"]: r["recomputed_at"] for r in
                 conn.execute("SELECT t.name, c.recomputed_at FROM conclusions c "
                              "JOIN terminals t ON t.id=c.terminal_id")}

    # 只给 PC-A 补新记录（睡眠跨过秒边界，确保 recomputed_at 可区分）
    import time as _time
    _time.sleep(1.1)
    stats = services.submit_pio_batch(db_path, [
        {"terminal": "PC-A", "seq": 1, "router": "R2",
         "received_at": "2026-11-20T01:30:00Z", "valid_lifetime": 8000},
    ])
    assert stats["recomputed"] == ["PC-A"]
    with get_db(db_path) as conn:
        second = {r["name"]: r["recomputed_at"] for r in
                  conn.execute("SELECT t.name, c.recomputed_at FROM conclusions c "
                               "JOIN terminals t ON t.id=c.terminal_id")}
    assert second["PC-A"] != first["PC-A"]
    assert second["PC-B"] == first["PC-B"]  # 其他终端结论时间戳不动


# ---------------------------------------------------------------- 并发补报

def test_concurrent_batches_both_commit_without_lost_updates(client):
    _setup_round(client)
    _add_terminal(client, "PC-A", "2026-11-20T04:00:00Z")
    db_path = client.application.config["AUDIT_DB"]
    errors = []

    def worker(seq, router):
        try:
            services.submit_pio_batch(db_path, [
                {"terminal": "PC-A", "seq": seq, "router": router,
                 "received_at": f"2026-11-20T01:{seq:02d}:00Z",
                 "valid_lifetime": 9000 + seq},
            ])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i, f"R{i}")) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    with get_db(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM pio_records").fetchone()["c"] == 8
        # 重算覆盖全部 8 条：按接收时刻重放，最后一条 seq7 的寿命最大且 >2h → e1
        decisions = conn.execute("SELECT rule FROM pio_decisions ORDER BY pio_id").fetchall()
    assert len(decisions) == 8


# ---------------------------------------------------------------- 页面渲染

def test_html_pages_render(client):
    _setup_round(client)
    _add_terminal(client, "PC-A", "2026-11-20T04:00:00Z")
    _pio(client, [
        {"terminal": "PC-A", "seq": 0, "router": "R1",
         "received_at": "2026-11-20T01:00:00Z", "valid_lifetime": 3600},
    ])
    for path in ("/", "/setup", "/pio"):
        resp = client.get(path)
        assert resp.status_code == 200
    resp = client.get("/terminals/1")
    assert resp.status_code == 200
    assert "§5.5.3".encode() in resp.data
    assert b"e3" in resp.data


def test_reset_clears_everything(client):
    _setup_round(client)
    _add_terminal(client, "PC-A", "2026-11-20T04:00:00Z")
    resp = client.post("/reset")
    assert resp.status_code == 302
    with get_db(client.application.config["AUDIT_DB"]) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM rounds").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM terminals").fetchone()["c"] == 0
