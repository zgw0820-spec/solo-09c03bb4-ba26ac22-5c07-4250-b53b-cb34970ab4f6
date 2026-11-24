"""IPv6 换前缀旧地址到期核查应用包。"""
import os

from .db import init_db


def create_app(test_config=None):
    from flask import Flask

    app = Flask(__name__, instance_relative_config=False)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("AUDIT_SECRET_KEY", "dev-secret-change-me"),
        AUDIT_DB=os.environ.get("AUDIT_DB", os.path.join(app.root_path, "..", "data", "audit.db")),
    )
    if test_config:
        app.config.update(test_config)

    os.makedirs(os.path.dirname(os.path.abspath(app.config["AUDIT_DB"])), exist_ok=True)
    init_db(app.config["AUDIT_DB"])

    from .routes import bp

    app.register_blueprint(bp)
    return app
