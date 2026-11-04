from fastapi import FastAPI

app = FastAPI(title="Agent Runtime Gateway", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
