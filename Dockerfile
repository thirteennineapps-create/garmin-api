FROM python:3.11-slim

WORKDIR /app

# Install deps first so this layer caches between deploys
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The garminconnect/ folder here is a vendored local copy (not the pip
# package pinned in requirements.txt) - main.py's `import garminconnect`
# resolves to this directory because it sits next to main.py, exactly as
# it does when running `uvicorn main:app` locally.
COPY garminconnect ./garminconnect
COPY main.py ./

EXPOSE 8081

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8081"]
