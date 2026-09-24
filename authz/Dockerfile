# Sidecar image for the LangSmith LLM auth proxy: Python standard library only, nothing to install.
#   docker build --platform linux/amd64 -t <registry>/langsmith-llm-authz:1 authz/
#   docker push <registry>/langsmith-llm-authz:1
FROM python:3.12-slim
COPY authz.py /app/authz.py
USER 65532:65532
EXPOSE 10002
CMD ["python", "/app/authz.py"]
