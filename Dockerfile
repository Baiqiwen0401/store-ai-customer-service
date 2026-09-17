FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
ENV STORE_AI_HOST=0.0.0.0 STORE_AI_PORT=8000
EXPOSE 8000
CMD ["python", "app.py"]
