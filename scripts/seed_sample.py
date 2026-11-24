"""灌入可试的样例：一轮核查 + 4 台终端 + 乱序 PIO（覆盖 RFC 4862 §5.5.3 各分支）。

用法：python -m scripts.seed_sample
仅在数据库为空（未建立 round）时执行，避免覆盖已有核查。
"""
from app import create_app
from app import services

SNAPSHOT = "2026-11-20T00:00:00Z"
CHECK = "2026-11-20T06:00:00Z"

# (终端, 快照旧地址到期, [PIO...])；PIO 故意乱序、序号不连续
TERMINALS = [
    (
        "PC-A",
        "2026-11-20T04:00:00Z",  # 01:00 时剩余 10800s > 2h
        [
            # 01:00 收到短寿命 3600s：不大于剩余、也不大于 2h → (e).3 截断到 03:00（提前到期）
            {"terminal": "PC-A", "seq": 0, "router": "R1",
             "received_at": "2026-11-20T01:00:00Z", "valid_lifetime": 3600},
        ],
    ),
    (
        "PC-B",
        "2026-11-20T02:00:00Z",  # 01:00 剩余 3600s ≤ 2h
        [
            # 乱序先补 01:30 的：剩余 1800s ≤ 7200，寿命 600s → (e).2 忽略
            {"terminal": "PC-B", "seq": 1, "router": "R2",
             "received_at": "2026-11-20T01:30:00Z", "valid_lifetime": 600},
            # 01:00 短寿命 600s → (e).2 忽略（剩余 3600s），到期仍 02:00
            {"terminal": "PC-B", "seq": 0, "router": "R1",
             "received_at": "2026-11-20T01:00:00Z", "valid_lifetime": 600},
        ],
    ),
    (
        "PC-C",
        "2026-11-20T02:00:00Z",
        [
            # 01:00 长寿命 10000s > 7200 → (e).1 重设，到期 01:00+10000s ≈ 03:46:40
            {"terminal": "PC-C", "seq": 0, "router": "R1",
             "received_at": "2026-11-20T01:00:00Z", "valid_lifetime": 10000},
            # 02:00 时剩余 6400s ≤ 7200，短寿命 300s → (e).2 忽略，防 DoS 提前到期
            {"terminal": "PC-C", "seq": 1, "router": "R2",
             "received_at": "2026-11-20T02:00:00Z", "valid_lifetime": 300},
        ],
    ),
    (
        "PC-D",
        None,  # 快照时无旧地址
        [
            # 01:00 寿命 0 → (d) 不形成地址
            {"terminal": "PC-D", "seq": 0, "router": "R1",
             "received_at": "2026-11-20T01:00:00Z", "valid_lifetime": 0},
            # 02:00 寿命 14400s → (d) 形成地址，到期 06:00，核查时刻恰好到期 → 不算持有
            {"terminal": "PC-D", "seq": 1, "router": "R1",
             "received_at": "2026-11-20T02:00:00Z", "valid_lifetime": 14400},
            # 03:00 寿命 86400s > 剩余 10800s → (e).1 重设，到期次日 03:00 → 核查时持有
            {"terminal": "PC-D", "seq": 2, "router": "R2",
             "received_at": "2026-11-20T03:00:00Z", "valid_lifetime": 86400},
        ],
    ),
]


def seed(db_path: str) -> dict:
    from app.db import get_db, init_db

    init_db(db_path)
    conn = get_db(db_path)
    try:
        if services.get_round(conn) is not None:
            raise services.ValidationError("数据库已存在一轮核查，样例未灌入（如需演示请先清空）")
        services.create_round(conn, SNAPSHOT, CHECK)
        for name, expiry, _ in TERMINALS:
            services.add_terminal(conn, name, expiry)
    finally:
        conn.close()

    all_pio = [pio for _, _, pios in TERMINALS for pio in pios]
    stats = services.submit_pio_batch(db_path, all_pio)
    return stats


if __name__ == "__main__":
    app = create_app()
    result = seed(app.config["AUDIT_DB"])
    print("样例已灌入：", result)
