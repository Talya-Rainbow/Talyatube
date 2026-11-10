FROM python:3.12-slim
WORKDIR /app
COPY server.py .
COPY static ./static
RUN mkdir -p data uploads
ENV PORT=8080
EXPOSE 8080
CMD ["python3", "server.py"]
