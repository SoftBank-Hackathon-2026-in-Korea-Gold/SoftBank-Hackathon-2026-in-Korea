import win32com.client
from flask import Flask

app = Flask(__name__)


@app.get("/report")
def report():
    excel = win32com.client.Dispatch("Excel.Application")
    return {"version": excel.Version}
