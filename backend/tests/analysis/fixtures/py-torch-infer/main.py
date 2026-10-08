import torch
from fastapi import FastAPI

app = FastAPI()
model = torch.nn.Linear(4, 1)


@app.post("/predict")
def predict(x: list[float]):
    with torch.no_grad():
        return {"y": model(torch.tensor(x)).item()}
