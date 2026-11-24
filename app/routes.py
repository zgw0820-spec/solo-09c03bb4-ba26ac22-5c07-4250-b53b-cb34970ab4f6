"""HTTP 路由：服务端渲染页面 + 等价 JSON API。"""
from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from . import services
from .db import get_db
from .timeutil import format_utc

bp = Blueprint("main", __name__)


def db():
    if "db" not in g:
        g.db = get_db(current_app.config["AUDIT_DB"])
    return g.db


@bp.teardown_request
def _close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@bp.app_template_filter("utc")
def _utc_filter(value):
    return format_utc(value)


# ---------------------------------------------------------------- 页面

@bp.route("/")
def index():
    rnd, rows = services.results_overview(db())
    return render_template("index.html", rnd=rnd, rows=rows)


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if request.method == "POST":
        try:
            services.create_round(db(), request.form.get("snapshot_at"), request.form.get("check_at"))
            flash("本轮核查已建立：快照/核查 UTC 时刻已保存", "ok")
        except services.ValidationError as exc:
            flash(exc.message, "err")
        return redirect(url_for("main.setup"))
    rnd = services.get_round(db())
    return render_template("setup.html", rnd=rnd)


@bp.route("/terminals", methods=["POST"])
def add_terminal():
    try:
        services.add_terminal(
            db(), request.form.get("name"), request.form.get("snapshot_expiry")
        )
        flash(f"终端 {request.form.get('name', '').strip()} 已登记，初始结论=快照基线", "ok")
    except services.ValidationError as exc:
        flash(exc.message, "err")
    return redirect(url_for("main.index"))


@bp.route("/terminals/<int:terminal_id>")
def terminal_detail(terminal_id):
    detail = services.terminal_detail(db(), terminal_id)
    if detail is None:
        flash("终端不存在", "err")
        return redirect(url_for("main.index"))
    return render_template("terminal.html", **detail)


@bp.route("/pio")
def pio_form():
    rnd = services.get_round(db())
    terminals = services.list_terminals(db())
    return render_template("pio.html", rnd=rnd, terminals=terminals)


@bp.route("/pio-batch", methods=["POST"])
def pio_batch_form():
    rows = _rows_from_form(request.form)
    try:
        stats = services.submit_pio_batch(current_app.config["AUDIT_DB"], rows)
        flash(
            f"整批生效：新插入 {stats['inserted']} 条，原样重报跳过 "
            f"{stats['duplicates_skipped']} 条，重算终端："
            + (", ".join(stats["recomputed"]) or "无"),
            "ok",
        )
    except services.ValidationError as exc:
        for re_ in exc.row_errors:
            flash(f"{re_['row']} 行（{re_['terminal']}）：" + "；".join(re_["errors"]), "err")
        if not exc.row_errors:
            flash(exc.message, "err")
    return redirect(url_for("main.pio_form"))


def _rows_from_form(form):
    names = form.getlist("terminal")
    out = []
    for i in range(len(names)):
        if not names[i].strip():
            continue  # 表单中的空行直接忽略
        out.append(
            {
                "terminal": names[i],
                "seq": form.getlist("seq")[i],
                "router": form.getlist("router")[i],
                "received_at": form.getlist("received_at")[i],
                "valid_lifetime": form.getlist("valid_lifetime")[i],
            }
        )
    return out


@bp.route("/reset", methods=["POST"])
def reset():
    services.reset_all(current_app.config["AUDIT_DB"])
    flash("已清空本轮全部数据，可重新建立核查", "ok")
    return redirect(url_for("main.index"))


# ---------------------------------------------------------------- JSON API

@bp.post("/api/round")
def api_create_round():
    data = request.get_json(force=True, silent=True) or {}
    try:
        row = services.create_round(db(), data.get("snapshot_at"), data.get("check_at"))
    except services.ValidationError as exc:
        return jsonify(error=exc.message), 400
    return jsonify(id=row["id"], snapshot_at=format_utc(row["snapshot_at"]),
                   check_at=format_utc(row["check_at"])), 201


@bp.post("/api/terminals")
def api_add_terminal():
    data = request.get_json(force=True, silent=True) or {}
    try:
        terminal_id = services.add_terminal(
            db(), data.get("name"), data.get("snapshot_expiry")
        )
    except services.ValidationError as exc:
        return jsonify(error=exc.message), 400
    return jsonify(id=terminal_id), 201


@bp.post("/api/pio-batch")
def api_pio_batch():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, list):
        return jsonify(error="请求体必须是 PIO 对象数组"), 400
    try:
        stats = services.submit_pio_batch(current_app.config["AUDIT_DB"], data)
    except services.ValidationError as exc:
        return jsonify(error=exc.message, row_errors=exc.row_errors), 409
    return jsonify(stats), 200


@bp.get("/api/results")
def api_results():
    rnd, rows = services.results_overview(db())
    return jsonify(
        round=None if rnd is None else {
            "snapshot_at": format_utc(rnd["snapshot_at"]),
            "check_at": format_utc(rnd["check_at"]),
        },
        terminals=[
            {
                "name": r["name"],
                "snapshot_expiry": format_utc(r["snapshot_expiry"]),
                "expiry_at": format_utc(r["expiry_at"]),
                "alive_at_check": bool(r["alive_at_check"]),
                "source": r["source"],
                "last_change": None
                if r["last_pio_id"] is None
                else {
                    "router": r["last_router"],
                    "seq": r["last_seq"],
                    "received_at": format_utc(r["last_received_at"]),
                    "valid_lifetime": r["last_valid_lifetime"],
                },
                "pio_count": r["pio_count"],
            }
            for r in rows
        ],
    )
