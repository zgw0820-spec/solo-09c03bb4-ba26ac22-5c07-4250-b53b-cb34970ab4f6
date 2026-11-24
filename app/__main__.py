"""开发/容器入口：python -m app 或 flask --app app run。"""
from . import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
