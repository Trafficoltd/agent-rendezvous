FROM python:3.12-slim

WORKDIR /srv

COPY mvp/requirements.txt /srv/mvp/requirements.txt
RUN pip install --no-cache-dir -r /srv/mvp/requirements.txt

COPY mvp/app /srv/mvp/app
COPY mvp/agents /srv/mvp/agents
COPY mvp/telegram /srv/mvp/telegram
COPY docs /srv/docs
COPY schemas /srv/schemas

RUN mkdir -p /srv/mvp/data

WORKDIR /srv/mvp
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
