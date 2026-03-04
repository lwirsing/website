FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

RUN apt-get update && apt-get install -y --no-install-recommends nginx gettext-base \
    && rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 10000

CMD ["sh", "-c", "streamlit run home_commute_app.py --server.address=0.0.0.0 --server.port=8501 --server.baseUrlPath=homesearch & rm -f /etc/nginx/conf.d/default.conf /etc/nginx/sites-enabled/default && envsubst '$PORT' < /app/nginx.default.conf.template > /etc/nginx/conf.d/default.conf && nginx -g 'daemon off;'"]
