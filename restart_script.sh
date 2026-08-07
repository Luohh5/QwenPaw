lsof -ti :8088 | xargs kill -9

cd console
npm install && npm run build

cd ..
make install-dev
python3 -m qwenpaw app