# 寄席瓦版 番組データ

iPhone アプリ「寄席瓦版」用に、東京の定席4軒（鈴本演芸場・新宿末廣亭・浅草演芸ホール・池袋演芸場）の番組を毎日1回取り込み、JSON で公開します。

- 公開先: https://yukuma-goseahawks.github.io/yose-kawaraban-data/ProgramData.json
- 更新: 毎日 6:00（日本時間）に GitHub Actions が `import_programs.py` を実行
- 取得元: 各寄席の公式サイト、落語協会・落語芸術協会のサイト（番組・芸人名簿）

番組の内容は各取得元のものです。出演者は変更されることがあります。

手元で動かす: `python3 import_programs.py`（結果は data/ProgramData.json）
