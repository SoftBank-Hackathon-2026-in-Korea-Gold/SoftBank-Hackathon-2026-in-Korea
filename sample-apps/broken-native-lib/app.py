import os
import platform

from flask import Flask, Response, jsonify
from lxml import etree

app = Flask(__name__)


@app.route("/")
def index():
    root = etree.Element("hello", arch=platform.machine(), port=os.environ.get("PORT", "8080"))
    root.text = "hello from flask + lxml"
    return Response(etree.tostring(root, xml_declaration=True, encoding="UTF-8"), mimetype="application/xml")


@app.route("/health")
def health():
    return jsonify(status="ok"), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
