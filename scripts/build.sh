#!/bin/bash
set -e
echo "Starting build process..."

echo "Building backend..."
cd backend
if [ ! -d "venv" ]; then
  python3 -m venv venv
fi
source venv/bin/activate
pip install -r requirements.txt
pytest
cd ..

echo "Building frontend..."
cd frontend
npm run build
cd ..

echo "Build complete."
