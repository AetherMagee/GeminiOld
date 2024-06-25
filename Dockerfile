FROM python:3.12-slim
LABEL authors="AetherMagee"
WORKDIR /bot
COPY requirements.txt .
RUN pip3 install -r requirements.txt
COPY . .
RUN touch .docker
ENTRYPOINT ["python3", "/bot/main.py"]