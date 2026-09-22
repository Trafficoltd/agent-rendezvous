FROM python:3.12-slim
WORKDIR /srv

COPY mvp/requirements.txt /srv/mvp/requirements.txt
RUN pip install --no-cache-dir -r /srv/mvp/requirements.txt

COPY mvp/app /srv/mvp/app
COPY docs /srv/docs
COPY schemas /srv/schemas

WORKDIR /srv/mvp
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
