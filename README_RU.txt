BestChess V4 Server Analyzer

Замените на PythonAnywhere файлы:
- app.py
- requirements.txt
- templates/index.html

Stockfish должен лежать по пути:
/home/Nurislombek/chess_analyzer/bin/stockfish/stockfish-ubuntu-x86-64
или задайте переменную STOCKFISH_PATH в WSGI.

После замены файлов:
1) git pull на PythonAnywhere или загрузите файлы вручную
2) pip install -r requirements.txt
3) Reload web app
