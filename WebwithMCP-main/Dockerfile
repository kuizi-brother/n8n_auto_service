# 指定基础映像
FROM python:3.12

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码到容器
COPY . .

# 暴露应用端口（如果需要）
EXPOSE 8003

# 容器启动命令
CMD ["python", "backend/main.py"]