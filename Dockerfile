# Образ нужен ровно для одного: чтобы локальная база и проверки поднимались
# одной командой на машине, где ничего не настроено. Живой контур в контейнере
# не работает — хуки зовёт агент из хозяйской системы, а не docker.
FROM python:3.11-slim

# Питон в контейнере не буферизует вывод и не сорит .pyc: журнал должен быть
# виден сразу, иначе падение выглядит как зависание.
# XMEM_BACKEND здесь не задаём нарочно: окружение сильнее файла рубильников,
# и заданный в образе путь наружу молча отменял бы ./bin/xmem backend.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    XMEM_LOCAL_PATH=/state/memory.db

WORKDIR /app

# Зависимости отдельным слоем: код меняется каждый ход, список — раз в месяц.
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY . .

# База лежит в томе, а не в слое образа: пересборка не должна стирать память.
VOLUME ["/state"]

CMD ["python3", "-m", "storage.db", "counts"]
