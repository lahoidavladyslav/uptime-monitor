FROM python:3.13-slim
RUN pip install --no-cache-dir "aiohttp>=3.9"
WORKDIR /app
COPY fake_target.py .
EXPOSE 8080
CMD ["python", "fake_target.py"]
