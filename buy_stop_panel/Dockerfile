FROM python:3.12-slim
WORKDIR /app
COPY app.py trigger.py index.html ./
RUN useradd --create-home paper && chown -R paper:paper /app
USER paper
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "app.py"]
