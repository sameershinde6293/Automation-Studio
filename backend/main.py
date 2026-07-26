from fastapi import FastAPI

app = FastAPI(title="Creator OS Backend", version="0.1.0")

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Creator OS Backend is running"}

@app.get("/health")
def health_check():
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
