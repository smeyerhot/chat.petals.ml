# Petals Chat
docker build -t chat-app .

docker run --net host --ipc host --gpus all --rm --volume petals-cache:/cache chat-app 