from fastapi import FastAPI

app = FastAPI()


@app.get("/api/items")
def items():
    return {"items": ["a", "b", "c"]}
