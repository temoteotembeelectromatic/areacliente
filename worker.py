import time

from app import cleanup_expired_pdf_jobs, process_next_pdf_job


def main():
    last_cleanup = 0
    while True:
        now = time.monotonic()
        if now - last_cleanup >= 600:
            cleanup_expired_pdf_jobs()
            last_cleanup = now
        processed = process_next_pdf_job()
        time.sleep(1 if processed else 3)


if __name__ == "__main__":
    main()
