#!/bin/bash

ROUTER_NAME=$1
PORT=$2

if [ -z "$ROUTER_NAME" ] || [ -z "$PORT" ]; then
  echo "Usage: bash install-router.sh router-name port"
  exit 1
fi

BASE_DIR="/home/aman/routers"
ROUTER_DIR="$BASE_DIR/$ROUTER_NAME"
REPO_URL="https://github.com/technicalboy2023/$ROUTER_NAME"

echo "----------------------------------"
echo "Installing router: $ROUTER_NAME"
echo "Port: $PORT"
echo "----------------------------------"

# install packages
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl

# create routers folder
mkdir -p $BASE_DIR
cd $BASE_DIR

# clone or update repo
if [ -d "$ROUTER_DIR" ]; then
  echo "Router already exists, updating..."
  cd $ROUTER_DIR
  git pull
else
  echo "Cloning router from GitHub..."
  git clone $REPO_URL
  cd $ROUTER_DIR
fi

# create venv
echo "Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

# upgrade pip
pip install --upgrade pip

# install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# check env file
if [ ! -f ".env" ]; then
  echo "Creating empty .env file..."
  touch .env
fi

# create systemd service
echo "Creating systemd service..."

SERVICE_FILE="/etc/systemd/system/$ROUTER_NAME.service"

sudo bash -c "cat > $SERVICE_FILE" <<EOF
[Unit]
Description=$ROUTER_NAME
After=network.target

[Service]
User=aman
WorkingDirectory=$ROUTER_DIR
ExecStart=$ROUTER_DIR/venv/bin/uvicorn router:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# reload systemd
sudo systemctl daemon-reload

# enable + start service
sudo systemctl enable $ROUTER_NAME
sudo systemctl restart $ROUTER_NAME

echo "----------------------------------"
echo "Router installed successfully"
echo "Router: $ROUTER_NAME"
echo "Port: $PORT"
echo "Directory: $ROUTER_DIR"
echo ""
echo "Edit API keys:"
echo "nano $ROUTER_DIR/.env"
echo ""
echo "Check status:"
echo "systemctl status $ROUTER_NAME"
echo "----------------------------------"
