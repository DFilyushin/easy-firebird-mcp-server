# Firebird MCP Server

MCP-сервер для работы с базами данных **Firebird** прямо из Claude. 

## Установка

```bash
pip install fdb mcp
```

## Настройка

Настройка через переменные окружения:

| Переменная    | По умолчанию  | Описание                              |
|---------------|---------------|---------------------------------------|
| `FB_HOST`     | `localhost`   | Хост Firebird-сервера                 |
| `FB_PORT`     | `3050`        | Порт                                  |
| `FB_DATABASE` | *(обязательно)* | Путь к базе данных или алиас        |
| `FB_USER`     | `SYSDBA`      | Пользователь                          |
| `FB_PASSWORD` | `masterkey`   | Пароль                                |
| `FB_CHARSET`  | `UTF8`        | Кодировка                             |
| `FB_ROLE`     | *(пусто)*     | Роль (необязательно)                  |
| `FB_ALLOW_DDL`| `1`           | Разрешить выполнение DDL (схема). `0` — запретить |

## Запуск

```bash
export FB_HOST=localhost
export FB_DATABASE=/var/lib/firebird/data/mydb.fdb
export FB_USER=SYSDBA
export FB_PASSWORD=masterkey

python server.py
```

## Подключение к Claude Desktop

Добавьте в файл `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "firebird": {
      "command": "python",
      "args": ["/path/to/firebird-mcp/server.py"],
      "env": {
        "FB_HOST": "localhost",
        "FB_PORT": "3050",
        "FB_DATABASE": "/path/to/your/database.fdb",
        "FB_USER": "SYSDBA",
        "FB_PASSWORD": "masterkey",
        "FB_CHARSET": "UTF8"
      }
    }
  }
}
```

Пример настройки через виртуальное окружение:
```json

	"mcpServers": {
    "mcp_name": {
      "command": "/home/user/easy-mcp-server/.venv/Scripts/python.exe",
      "args": ["/home/user/easy-mcp-server/server.py"],
      "env": {
        "FB_HOST": "localhost",
        "FB_PORT": "3050",
        "FB_DATABASE": "/var/database/database.fdb",
        "FB_USER": "SYSDBA",
        "FB_PASSWORD": "masterkey",
        "FB_CHARSET": "UTF8",
        "FB_ALLOW_DDL": "true"
      }
    }
  }
```



Расположение файла конфига:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/claude/claude_desktop_config.json`

## Доступные инструменты

| Инструмент             | Описание                                                  |
|------------------------|-----------------------------------------------------------|
| `get_db_info`          | Информация о базе (версия FB, кол-во таблиц, процедур)   |
| `list_tables`          | Список всех пользовательских таблиц                       |
| `describe_table`       | Структура таблицы: колонки, типы, PK, индексы            |
| `list_views`           | Список представлений (VIEW) с исходным SQL                |
| `list_procedures`      | Список хранимых процедур                                  |
| `get_procedure_source` | Исходный код и параметры хранимой процедуры              |
| `execute_query`        | Выполнение SELECT-запросов (с поддержкой параметров `?`) |
| `execute_dml`          | Выполнение одного INSERT / UPDATE / DELETE в транзакции (COMMIT при успехе, ROLLBACK при ошибке); поддержка `RETURNING` |
| `execute_many`         | Пакетное выполнение одного DML-запроса для набора параметров в одной транзакции (всё или ничего) |
| `execute_ddl`          | Выполнение DDL: CREATE / ALTER / DROP / RECREATE и т.п. (требует `FB_ALLOW_DDL=1`) |

> **Внимание:** DML- и DDL-инструменты строго разделены. `execute_dml` / `execute_many`
> принимают только INSERT/UPDATE/DELETE/MERGE/EXECUTE, а `execute_ddl` — только
> операторы изменения схемы. DDL необратим (например, `DROP`), поэтому управляется
> флагом `FB_ALLOW_DDL` (по умолчанию включён; установите `0`, чтобы запретить).

## Примеры использования в Claude

После подключения можно спрашивать:

- *«Покажи список таблиц в базе данных»*
- *«Опиши структуру таблицы CUSTOMERS»*
- *«Выполни запрос: SELECT * FROM ORDERS WHERE STATUS = 'NEW'»*
- *«Какие хранимые процедуры есть в базе?»*
- *«Покажи исходный код процедуры CALC_TOTAL»*
- *«Добавь запись в таблицу CUSTOMERS и верни сгенерированный ID»* (INSERT … RETURNING)
- *«Создай таблицу LOG (ID INTEGER, MSG VARCHAR(200))»* (DDL)

## Требования

- Python 3.9+
- Firebird 2.5 / 3.0 / 4.0 / 5.0
- Установленный клиент Firebird (fbclient.dll / libfbclient.so)
