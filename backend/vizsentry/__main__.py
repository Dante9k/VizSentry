import os
import uvicorn


def main():
    uvicorn.run('vizsentry.app:create_app', factory=True, host='127.0.0.1', port=int(os.environ.get('VIZSENTRY_PORT', '8765')), access_log=False)


if __name__ == '__main__':
    main()
