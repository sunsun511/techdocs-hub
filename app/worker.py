import logging
import threading
from .chunking import chunk_document

log = logging.getLogger("techdocs.worker")


class Worker:
    def __init__(self, store, models, max_chunks=2500):
        self.store, self.models, self.max_chunks = store, models, max_chunks
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.processing_lock = threading.Lock()
        self.thread = None
        self.recovered = self.store.recover_tasks()

    def start(self):
        self.thread = threading.Thread(target=self.run, name="techdocs-index-worker", daemon=True)
        self.thread.start()

    def notify(self):
        self.wake_event.set()

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=10)

    def run(self):
        while not self.stop_event.is_set():
            if not self.process_once():
                self.wake_event.wait(timeout=.5)
                self.wake_event.clear()

    def process_once(self):
        with self.processing_lock:
            task = self.store.claim_task()
            if not task:
                return False
            try:
                version = task["version"]
                chunks = chunk_document(version["content"], version["format"], version["title"])
                if not chunks:
                    raise ValueError("文档没有可索引正文，请检查内容和格式")
                if len(chunks) > self.max_chunks:
                    raise ValueError("文档分块超过限制，请拆成多个文档")
                matrix = self.models.embed([c["heading"] + "\n" + c["text"] for c in chunks], "passage: ")
                if self.stop_event.is_set():
                    # Keep task running so next process safely recovers it.
                    return False
                if matrix is not None:
                    for chunk, vector in zip(chunks, matrix):
                        chunk["vector"] = vector.astype("float32").tobytes()
                published = self.store.publish(task, chunks, self.models.model_tag)
                log.info("index_task_finished task=%s chunks=%d published=%s", task["id"], len(chunks), published)
            except Exception as exc:
                self.store.fail(task, exc)
                log.exception("index_task_failed task=%s", task["id"])
            return True
