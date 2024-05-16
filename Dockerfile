FROM python:3.12
LABEL authors="AetherMagee"
WORKDIR /bot
COPY requirements.txt .
RUN pip3 install -r requirements.txt
COPY . .
RUN touch .docker
ENTRYPOINT ["/bot/start.sh"]