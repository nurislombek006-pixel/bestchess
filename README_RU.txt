Chess Analyzer Pro Server

Это НЕ браузерный анализатор. Анализ делает сервер, поэтому одна и та же партия при одинаковом Stockfish и глубине даст одинаковый результат на iPhone, Android и ПК.

Как запустить на Windows:
1) Установи Python 3.10+
2) Скачай Stockfish: https://stockfishchess.org/download/
3) Распакуй stockfish.exe, например в C:\stockfish\stockfish.exe
4) Открой app.py и проверь STOCKFISH_CANDIDATES или задай переменную:
   set STOCKFISH_PATH=C:\stockfish\stockfish.exe
5) Запусти start_windows.bat
6) Открой в браузере: http://127.0.0.1:5000

Как запустить на VPS/Linux:
1) sudo apt update && sudo apt install -y stockfish python3-pip
2) pip install -r requirements.txt
3) python3 app.py
4) Открой http://IP_СЕРВЕРА:5000

Важно:
- Для одинакового результата на всех устройствах все пользователи должны пользоваться одним сервером.
- Глубина 14-16 быстрее, глубина 18-20 точнее, но дольше.
- Chess.com использует закрытые формулы, поэтому 1:1 повторить нельзя, но здесь анализ стабильный и значительно правильнее браузерного.
