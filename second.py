# main.py (patched)
import requests
import threading
import os
import sys
import time
import pyperclip
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
from tkinter import filedialog
import uuid
import asyncio
import aiohttp
import aiofiles
import queue
import json
import shutil

# --- ค่าคงที่ ---
CONCURRENT_WORKERS = 16
DEFAULT_CHUNK_SIZE = 1024 * 1024 * 1
MAX_RETRIES = 5
STATE_SAVE_INTERVAL = 5
MAX_CONCURRENT_JOBS = 3

# -------------------------------------------------------------------
# เอนจิ้นดาวน์โหลดพื้นฐาน (Fallback)
# -------------------------------------------------------------------
def download_basic(url, save_path, start_time=None):
    if start_time is None:
        start_time = time.monotonic()
    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(save_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        end_time = time.monotonic()
        duration = end_time - start_time
        print(f"ดาวน์โหลดพื้นฐานเสร็จสิ้น! เวลา: {duration:.2f} วินาที")
    except requests.exceptions.RequestException as e:
        print(f"เกิดข้อผิดพลาดในการดาวน์โหลดพื้นฐาน: {e}")

# -------------------------------------------------------------------
# เอนจิ้น Async หลัก (มี Pause/Resume, Resume semantics fixed)
# -------------------------------------------------------------------
async def get_file_info_async(session, url):
    try:
        async with session.head(url, allow_redirects=True) as r:
            r.raise_for_status()
            file_size = r.headers.get('Content-Length')
            accept_ranges = r.headers.get('Accept-Ranges')
            if not file_size:
                return None, False
            file_size = int(file_size)
            supports_ranges = (accept_ranges == 'bytes')
            print(f"เอนจิ้น Async: ขนาดไฟล์: {file_size / (1024*1024):.2f} MB, รองรับ Ranges: {supports_ranges}")
            return file_size, supports_ranges
    except Exception as e:
        print(f"เกิดข้อผิดพลาดในการตรวจสอบข้อมูลไฟล์: {e}")
        return None, False

def load_progress(state_file_path):
    if not os.path.exists(state_file_path):
        return None
    try:
        with open(state_file_path, 'r') as f:
            state = json.load(f)
            # ensure completed_chunks is a set (for fast membership)
            state['completed_chunks'] = set(state.get('completed_chunks', []))
            # paused flag optionally present
            state['paused'] = state.get('paused', False)
            print(f"โหลด {len(state['completed_chunks'])} chunks ที่เสร็จแล้วจากไฟล์สถานะ.")
            return state
    except Exception as e:
        print(f"ไฟล์สถานะเสียหาย เริ่มใหม่. Error: {e}")
        return None

async def save_progress_async(state_file_path, file_size, chunk_size, completed_chunks_set, paused=False):
    state = {
        'total_size': file_size,
        'chunk_size': chunk_size,
        'completed_chunks': list(completed_chunks_set),
        'paused': bool(paused)
    }
    try:
        async with aiofiles.open(state_file_path, 'w') as f:
            await f.write(json.dumps(state, indent=4))
    except Exception as e:
        print(f"เกิดข้อผิดพลาดในการบันทึกสถานะ: {e}")

async def state_saver(state_file_path, file_size, chunk_size, completed_chunks_set, resume_event: asyncio.Event, cancel_event: asyncio.Event):
    """บันทึกสถานะทุก interval และเมื่อ pause เกิดขึ้น จะบันทึกทันทีและรอ resume"""
    try:
        while True:
            # normal periodic save
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=STATE_SAVE_INTERVAL)
                # if cancel_event is set, break to perform final save in finally
                break
            except asyncio.TimeoutError:
                pass

            # detect paused state and save immediately
            paused = not resume_event.is_set()
            await save_progress_async(state_file_path, file_size, chunk_size, completed_chunks_set, paused=paused)
            print("[State Saver] บันทึกสถานะ... paused=", paused)

            # if paused, wait until resumed (resume_event.set())
            if paused:
                print("[State Saver] Paused, waiting for resume...")
                try:
                    await resume_event.wait()
                    print("[State Saver] Resumed.")
                except asyncio.CancelledError:
                    break

    except asyncio.CancelledError:
        print("[State Saver] ถูกยกเลิกระหว่างรอ")
    finally:
        print("[State Saver] บันทึกสถานะครั้งสุดท้ายก่อนจบ...")
        try:
            await save_progress_async(state_file_path, file_size, chunk_size, completed_chunks_set, paused=(not resume_event.is_set()))
        except Exception as e:
            print("[State Saver] ขณะบันทึกครั้งสุดท้ายเกิด error:", e)

async def file_writer(save_path, write_queue, progress_queue, completed_chunks_set, chunk_size, job_id, cancel_event: asyncio.Event):
    """เขียนข้อมูลลงดิสก์ แต่ข้าม chunk ที่บันทึกแล้ว"""
    # ensure file exists and open in r+b
    try:
        # create file if missing
        if not os.path.exists(save_path):
            # create sparse file or allocate space
            open(save_path, 'wb').close()
        async with aiofiles.open(save_path, 'r+b') as f:
            while True:
                try:
                    # allow cancellation
                    if cancel_event.is_set():
                        break
                    start_byte, data = await write_queue.get()
                    chunk_index = start_byte // chunk_size
                    if chunk_index in completed_chunks_set:
                        # already written, skip
                        write_queue.task_done()
                        continue
                    await f.seek(start_byte)
                    await f.write(data)
                    completed_chunks_set.add(chunk_index)
                    progress_queue.put_nowait((job_id, 'chunk_done', len(data)))
                    write_queue.task_done()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    print(f"[Writer] Error: {e}")
                    try:
                        write_queue.task_done()
                    except Exception:
                        pass
    except Exception as e:
        print(f"[Writer] ไม่สามารถเปิดไฟล์ {save_path}: {e}")

async def worker(worker_id, session, url, download_queue, write_queue, download_failed_event, resume_event: asyncio.Event, cancel_event: asyncio.Event):
    """worker: ก่อนเริ่ม chunk จะรอ resume_event (set => running)."""
    while True:
        try:
            # pause-resume control: await resume_event.wait() (blocks when cleared)
            await resume_event.wait()
            print(f"[Worker {worker_id}] started. resume_event={resume_event.is_set()}")
            if cancel_event.is_set():
                break

            start_byte, end_byte, retry_count = await download_queue.get()
            headers = {'Range': f'bytes={start_byte}-{end_byte}'}
            try:
                async with session.get(url, headers=headers) as r:
                    r.raise_for_status()
                    data = await r.content.read()
                    await write_queue.put((start_byte, data))
                download_queue.task_done()
            except Exception as e:
                # retry logic
                if retry_count < MAX_RETRIES and not cancel_event.is_set():
                    retry_count += 1
                    backoff = min(5, retry_count)
                    await asyncio.sleep(backoff)
                    await download_queue.put((start_byte, end_byte, retry_count))
                else:
                    print(f"!!! Worker {worker_id} ล้มเหลวถาวร chunk เริ่มต้น {start_byte}: {e}")
                    download_failed_event.set()
                try:
                    download_queue.task_done()
                except Exception:
                    pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Worker {worker_id}] Unexpected error: {e}")
            await asyncio.sleep(1)

async def download_dynamic_async(job_id, url, save_path, progress_queue, cancel_event: asyncio.Event, resume_event: asyncio.Event):
    """ฟังก์ชันจัดการดาวน์โหลดหลัก (ใช้ resume_event แทน pause_event)"""
    start_time = time.monotonic()
    state_file_path = save_path + ".progress.json"
    all_tasks = []
    chunks_to_download = 0

    try:
        async with aiohttp.ClientSession() as session:
            loaded_state = load_progress(state_file_path)
            completed_chunks_set = set()
            if loaded_state:
                completed_chunks_set = set(loaded_state.get('completed_chunks', set()))
                initial_downloaded_size = len(completed_chunks_set) * DEFAULT_CHUNK_SIZE
                progress_queue.put_nowait((job_id, 'initial_chunks', initial_downloaded_size))
                # if state says paused, clear resume_event to pause
                if loaded_state.get('paused', False):
                    resume_event.clear()

            file_size, supports_ranges = await get_file_info_async(session, url)
            if file_size is None:
                raise Exception("ไม่สามารถรับข้อมูลไฟล์ได้")
            if not supports_ranges:
                if os.path.exists(state_file_path):
                    os.remove(state_file_path)
                print("เซิร์ฟเวอร์ไม่รองรับ ranges, ใช้การดาวน์โหลดพื้นฐาน...")
                download_basic(url, save_path, start_time)
                progress_queue.put_nowait((job_id, 'done', f"เสร็จสิ้น ({time.monotonic() - start_time:.2f} วินาที)"))
                return

            if loaded_state and loaded_state.get('total_size') != file_size:
                print("!!! ขนาดไฟล์บนเซิร์ฟเวอร์เปลี่ยน! เริ่มดาวน์โหลดใหม่.")
                completed_chunks_set.clear()
                if os.path.exists(state_file_path):
                    os.remove(state_file_path)
                progress_queue.put_nowait((job_id, 'reset_progress', None))

            progress_queue.put_nowait((job_id, 'total_size', file_size))

            # check disk space (simple check)
            try:
                disk = shutil.disk_usage(os.path.dirname(save_path) or '.')
                free = disk.free
                if free < file_size * 0.9:
                    print(f"[{job_id}] Warning: พื้นที่ไม่พอจริงๆ (free={free} bytes).")
            except Exception:
                pass

            # prepare file: allocate if not exist
            if not os.path.exists(save_path) or os.path.getsize(save_path) != file_size:
                try:
                    # allocate sparse by seeking
                    with open(save_path, 'wb') as f:
                        if file_size > 0:
                            f.seek(file_size - 1)
                            f.write(b'\0')
                    print(f"[{job_id}] จองพื้นที่ไฟล์เรียบร้อย")
                except IOError as e:
                    raise Exception(f"ไม่สามารถเขียนไฟล์ได้: {e}")
            else:
                print(f"[{job_id}] ใช้ไฟล์ที่ดาวน์โหลดค้างไว้")

            download_queue = asyncio.Queue()
            write_queue = asyncio.Queue()
            download_failed_event = asyncio.Event()

            # prepare chunks to download
            chunks_to_download = 0
            current_byte = 0
            while current_byte < file_size:
                chunk_index = current_byte // DEFAULT_CHUNK_SIZE
                if chunk_index not in completed_chunks_set:
                    start_byte = current_byte
                    end_byte = min(current_byte + DEFAULT_CHUNK_SIZE - 1, file_size - 1)
                    await download_queue.put((start_byte, end_byte, 0))
                    chunks_to_download += 1
                current_byte += DEFAULT_CHUNK_SIZE

            if chunks_to_download == 0:
                print(f"[{job_id}] ไฟล์ดาวน์โหลดเสร็จสมบูรณ์แล้ว (จากครั้งก่อน).")
                progress_queue.put_nowait((job_id, 'done', "Already complete"))
            else:
                print(f"[{job_id}] ต้องดาวน์โหลดอีก {chunks_to_download} chunks.")
                writer_task = asyncio.create_task(
                    file_writer(save_path, write_queue, progress_queue, completed_chunks_set, DEFAULT_CHUNK_SIZE, job_id, cancel_event))
                workers = [asyncio.create_task(
                    worker(i + 1, session, url, download_queue, write_queue, download_failed_event, resume_event, cancel_event))
                    for i in range(CONCURRENT_WORKERS)]
                saver_task = asyncio.create_task(
                    state_saver(state_file_path, file_size, DEFAULT_CHUNK_SIZE, completed_chunks_set, resume_event, cancel_event))
                download_join_task = asyncio.create_task(download_queue.join())
                failed_event_task = asyncio.create_task(download_failed_event.wait())
                cancel_wait_task = asyncio.create_task(cancel_event.wait())

                all_tasks = workers + [writer_task, saver_task, download_join_task, failed_event_task, cancel_wait_task]

                print(f"[{job_id}] กำลังรอ ดาวน์โหลด/ล้มเหลว/ยกเลิก...")
                done, pending = await asyncio.wait(
                    [download_join_task, failed_event_task, cancel_wait_task],
                    return_when=asyncio.FIRST_COMPLETED)

                print(f"[{job_id}] กำลังหยุด workers และ watchers...")
                # cancel workers and pending tasks
                for t in workers + list(pending):
                    t.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

                if cancel_wait_task in done:
                    print(f"[{job_id}] ดาวน์โหลดถูกยกเลิก! กำลังล้างข้อมูล...")
                    progress_queue.put_nowait((job_id, 'error', "Cancelled"))
                    # cancel writer & saver
                    writer_task.cancel()
                    saver_task.cancel()
                    await asyncio.gather(writer_task, saver_task, return_exceptions=True)
                    # cleanup
                    try:
                        if os.path.exists(save_path):
                            os.remove(save_path)
                        if os.path.exists(state_file_path):
                            os.remove(state_file_path)
                    except Exception as e:
                        print(f"[{job_id}] เกิดข้อผิดพลาดขณะล้างข้อมูล: {e}")
                    return

                elif failed_event_task in done:
                    print(f"[{job_id}] ดาวน์โหลดล้มเหลวถาวร!")
                    progress_queue.put_nowait((job_id, 'error', "Download Failed"))
                    writer_task.cancel(); saver_task.cancel()
                    await asyncio.gather(writer_task, saver_task, return_exceptions=True)
                    return

                else:
                    # download_join_task finished
                    print(f"[{job_id}] ดาวน์โหลด chunks ทั้งหมดเสร็จสิ้น.")
                    print(f"[{job_id}] กำลังรอเขียนไฟล์ให้เสร็จ...")
                    await write_queue.join()
                    print(f"[{job_id}] เขียนไฟล์เสร็จสมบูรณ์.")
                    writer_task.cancel(); saver_task.cancel()
                    await asyncio.gather(writer_task, saver_task, return_exceptions=True)

            print(f"[{job_id}] กระบวนการดาวน์โหลดเสร็จสิ้น.")
            if os.path.exists(state_file_path):
                try:
                    os.remove(state_file_path)
                except Exception as e:
                    print(f"[{job_id}] เกิดข้อผิดพลาดในการลบไฟล์สถานะ: {e}")

            end_time = time.monotonic()
            duration = end_time - start_time
            progress_queue.put_nowait((job_id, 'done', f"เสร็จสิ้น ({duration:.2f} วินาที)"))

    except Exception as e:
        print(f"!!! [{job_id}] ตัวจัดการดาวน์โหลดล้มเหลว: {e}")
        progress_queue.put_nowait((job_id, 'error', f"Manager Error: {type(e).__name__}: {e}"))
    finally:
        print(f"[{job_id}] Entering finally block, cancelling remaining tasks...")
        for task in all_tasks:
            try:
                if task and not task.done():
                    task.cancel()
            except Exception:
                pass
        await asyncio.gather(*all_tasks, return_exceptions=True)
        print(f"[{job_id}] Exiting finally block.")

# -------------------------------------------------------------------
# คลาส Application หลัก
# -------------------------------------------------------------------
class ClipboardMonitor(threading.Thread):
    def __init__(self, new_url_queue, download_extensions, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.new_url_queue = new_url_queue
        self.download_extensions = download_extensions
        self.last_copied = ""
        self.stop_event = stop_event

    def run(self):
        print("[Clipboard Monitor] เริ่มทำงาน.")
        while not self.stop_event.is_set():
            try:
                copied_text = pyperclip.paste()
                if copied_text != self.last_copied:
                    self.last_copied = copied_text
                    text_to_check = copied_text.lower().strip()
                    if text_to_check.startswith(("http://", "https://")):
                        is_dl = any(text_to_check.split('?')[0].endswith(ext) for ext in self.download_extensions)
                        if is_dl:
                            self.new_url_queue.put(copied_text)
                time.sleep(1)
            except Exception as e:
                print(f"[Clipboard Monitor] Error: {e}")
                time.sleep(5)
        print("[Clipboard Monitor] หยุดทำงานแล้ว.")

class DownloadManager:
    def __init__(self, progress_queue):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.run_loop, daemon=True)
        self.progress_queue = progress_queue
        self.active_jobs = {}  # { job_id: {'future': Future, 'cancel': asyncio.Event, 'resume': asyncio.Event} }
        self.download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

    def run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self):
        self.thread.start()

    def stop(self):
        print("[Async Engine] กำลังหยุด...")
        # signal cancellation to active jobs
        for job_id, job_data in list(self.active_jobs.items()):
            if job_data.get('cancel') and not job_data['cancel'].is_set():
                self.loop.call_soon_threadsafe(job_data['cancel'].set)
        time.sleep(0.2)
        # stop loop
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        print("[Async Engine] หยุดแล้ว.")

    def submit_job(self, job_id, url, save_path):
        print(f"[Async Engine] กำลังส่งงาน: {job_id}")
        cancel_event = asyncio.Event()
        resume_event = asyncio.Event()
        resume_event.set()  # initially running

        async def _job_wrapper():
            acquired_semaphore = False
            try:
                self.progress_queue.put_nowait((job_id, 'status_update', 'Waiting...'))
                print(f"[{job_id}] Waiting for semaphore...")
                # wait for semaphore
                async with self.download_semaphore:
                    acquired_semaphore = True
                    print(f"[{job_id}] Acquired semaphore. Task starting...")
                    self.progress_queue.put_nowait((job_id, 'status_update', 'Starting...'))
                    await download_dynamic_async(job_id, url, save_path, self.progress_queue, cancel_event, resume_event)
                print(f"[{job_id}] Released semaphore. Task wrapper finished.")
            except asyncio.CancelledError:
                print(f"[{job_id}] Task ถูกยกเลิก (wrapper)")
            except Exception as e:
                print(f"!!! [{job_id}] Task ล้มเหลว (wrapper): {e}")
                self.progress_queue.put_nowait((job_id, 'error', f"Engine Error: {type(e).__name__}: {e}"))
            finally:
                if acquired_semaphore:
                    pass
                # Remove from active_jobs safely (schedule on loop thread)
                def _remove():
                    try:
                        self.active_jobs.pop(job_id, None)
                    except Exception:
                        pass
                self.loop.call_soon_threadsafe(_remove)

        # schedule wrapper but DO NOT block; store future handle
        future = asyncio.run_coroutine_threadsafe(_job_wrapper(), self.loop)
        self.active_jobs[job_id] = {'future': future, 'cancel': cancel_event, 'resume': resume_event}
        print(f"[Async Engine] งาน {job_id} ถูกเพิ่มเข้าคิว (รอ Semaphore)")

    def cancel_job(self, job_id):
        if job_id in self.active_jobs:
            print(f"[Async Engine] กำลังยกเลิกงาน: {job_id}")
            job_data = self.active_jobs[job_id]
            # set cancel flag
            self.loop.call_soon_threadsafe(job_data['cancel'].set)
            # also resume in case it's paused so it can observe cancel
            self.loop.call_soon_threadsafe(job_data['resume'].set)
        else:
            print(f"[Async Engine] ไม่พบงาน {job_id} สำหรับการยกเลิก.")

    def pause_job(self, job_id):
        if job_id in self.active_jobs:
            print(f"[Async Engine] กำลังหยุดงาน: {job_id}")
            job_data = self.active_jobs[job_id]
            self.loop.call_soon_threadsafe(job_data['resume'].clear)
        else:
            print(f"[Async Engine] ไม่พบงาน {job_id} สำหรับการหยุด.")

    def resume_job(self, job_id):
        if job_id in self.active_jobs:
            print(f"[Async Engine] กำลังทำต่อ: {job_id}")
            job_data = self.active_jobs[job_id]
            self.loop.call_soon_threadsafe(job_data['resume'].set)
        else:
            print(f"[Async Engine] ไม่พบงาน {job_id} สำหรับการทำต่อ.")

class MainApplication(tk.Tk):
    def __init__(self, download_extensions):
        super().__init__()
        self.title(f"Auto Queue Downloader (v16 - {MAX_CONCURRENT_JOBS} Concurrent)")
        self.geometry("700x450")
        self.new_url_queue = queue.Queue()
        self.progress_queue = queue.Queue()
        self.job_details = {}
        self.downloader = DownloadManager(self.progress_queue)
        self.clipboard_stop = threading.Event()
        self.monitor = None
        self.create_widgets()
        self.downloader.start()
        self.check_queues()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.monitor = ClipboardMonitor(self.new_url_queue, download_extensions, self.clipboard_stop)
        self.monitor.start()

    def create_widgets(self):
        add_frame = ttk.Frame(self, padding="10")
        add_frame.pack(fill=tk.X)
        ttk.Label(add_frame, text="URL:").pack(side=tk.LEFT, padx=(0, 5))
        self.manual_url_entry = ttk.Entry(add_frame)
        self.manual_url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.add_button = ttk.Button(add_frame, text="Add Download", command=self.add_manual_download)
        self.add_button.pack(side=tk.LEFT)

        tree_frame = ttk.Frame(self, padding="10")
        tree_frame.pack(fill=tk.BOTH, expand=True)
        columns = ("filename", "size", "progress", "status")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        self.tree.heading("filename", text="File Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("progress", text="Progress")
        self.tree.heading("status", text="Status")
        self.tree.column("filename", width=300)
        self.tree.column("size", width=80, anchor=tk.E)
        self.tree.column("progress", width=150)
        self.tree.column("status", width=100, anchor=tk.W)
        s = ttk.Style()
        try:
            s.theme_use('vista')
        except Exception:
            pass
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self.update_button_states)

        control_frame = ttk.Frame(self, padding="10")
        control_frame.pack(fill=tk.X)
        self.pause_resume_button = ttk.Button(control_frame, text="Pause", command=self.toggle_pause_resume, state=tk.DISABLED)
        self.pause_resume_button.pack(side=tk.LEFT, padx=5)
        self.start_button = ttk.Button(control_frame, text="Start/Restart", command=self.start_selected_download, state=tk.DISABLED)
        self.start_button.pack(side=tk.LEFT, padx=5)
        self.cancel_button = ttk.Button(control_frame, text="Cancel/Delete", command=self.cancel_selected_download, state=tk.DISABLED)
        self.cancel_button.pack(side=tk.LEFT, padx=5)
        self.update_button_states()

    def check_queues(self):
        try:
            while not self.new_url_queue.empty():
                url = self.new_url_queue.get_nowait()
                self.add_to_queue(url)
        except queue.Empty:
            pass
        try:
            while not self.progress_queue.empty():
                job_id, message_type, data = self.progress_queue.get_nowait()
                self.handle_progress_update(job_id, message_type, data)
        except queue.Empty:
            pass
        self.after(100, self.check_queues)

    def add_manual_download(self):
        url = self.manual_url_entry.get()
        if url:
            self.add_to_queue(url)
            self.manual_url_entry.delete(0, tk.END)

    def add_to_queue(self, url):
        filename = url.split('/')[-1].split('?')[0] or "unknown_file"
        job_id = str(uuid.uuid4())
        self.job_details[job_id] = {'url': url, 'save_path': None, 'total_size': 0, 'downloaded': 0, 'pbar': None, 'status': 'Queued'}
        item_id = self.tree.insert("", tk.END, iid=job_id, values=(filename, "?", 0, "Queued"))
        pbar = ttk.Progressbar(self.tree, orient='horizontal', mode='determinate', style="TProgressbar")
        self.job_details[job_id]['pbar'] = pbar
        print(f"GUI: เพิ่ม {filename} (Job ID: {job_id}) เข้าคิว")

    def start_selected_download(self):
        selected_items = self.tree.selection()
        if not selected_items:
            messagebox.showwarning("คำเตือน", "กรุณาเลือกไฟล์")
            return
        job_id = selected_items[0]
        if job_id not in self.job_details:
            return
        details = self.job_details[job_id]
        status = details['status']
        if status not in ("Queued", "Error", "Cancelled", "Download Failed", "Manager Error", "เก็บไฟล์ไว้"):
            messagebox.showwarning("คำเตือน", f"ไม่สามารถเริ่มงานในสถานะ '{status}' ได้")
            return
        url = details['url']
        filename = self.tree.item(job_id, "values")[0]
        initial_file = details.get('save_path') or filename
        save_path = filedialog.asksaveasfilename(initialfile=initial_file, title="เลือกที่บันทึกไฟล์")
        if not save_path:
            return
        details['save_path'] = save_path
        details['downloaded'] = 0
        details['status'] = 'Waiting...'
        self.tree.item(job_id, values=(filename, "?", 0, "Waiting..."))
        if details['pbar']:
            details['pbar'].config(value=0)
        print(f"GUI: กำลังส่ง {job_id} ไปรอ Semaphore ด้วย URL: {url}")
        self.downloader.submit_job(job_id, url, save_path)
        self.update_button_states()

    def cancel_selected_download(self):
        selected_items = self.tree.selection()
        if not selected_items:
            return
        job_id = selected_items[0]
        details = self.job_details.get(job_id)
        if not details:
            return
        print(f"GUI: Requesting cancel for {job_id}")
        self.downloader.cancel_job(job_id)
        if self.tree.exists(job_id):
            self.tree.delete(job_id)
        self.job_details.pop(job_id, None)
        print(f"GUI: ลบ {job_id} ออกจากคิว")
        self.update_button_states()

    def toggle_pause_resume(self):
        selected_items = self.tree.selection()
        if not selected_items:
            return
        job_id = selected_items[0]
        if job_id not in self.job_details:
            return
        details = self.job_details[job_id]
        status = details['status']
        # Pause if downloading, resume if paused
        if status == 'Downloading':
            self.downloader.pause_job(job_id)
            details['status'] = 'Paused'
            if self.tree.exists(job_id):
                self.tree.item(job_id, values=(*self.tree.item(job_id, "values")[:3], "Paused"))
            print(f"GUI: สั่งหยุด {job_id}")
        elif status == 'Paused':
            self.downloader.resume_job(job_id)
            details['status'] = 'Downloading'
            print(f"GUI: สั่งทำต่อ {job_id}")
        self.update_button_states()

    def update_button_states(self, event=None):
        selected_items = self.tree.selection()
        if not selected_items:
            self.start_button.config(state=tk.DISABLED)
            self.pause_resume_button.config(state=tk.DISABLED, text="Pause")
            self.cancel_button.config(state=tk.DISABLED)
            return
        job_id = selected_items[0]
        details = self.job_details.get(job_id)
        if not details:
            self.start_button.config(state=tk.DISABLED)
            self.pause_resume_button.config(state=tk.DISABLED, text="Pause")
            self.cancel_button.config(state=tk.DISABLED)
            return
        status = details.get('status', 'Unknown')
        can_start = status in ("Queued", "Error", "Cancelled", "Download Failed", "Manager Error", "เก็บไฟล์ไว้")
        can_pause_resume = status in ("Downloading", "Paused")
        can_cancel = status not in ("Done", "เสร็จสิ้น", "ลบแล้ว")
        self.start_button.config(state=tk.NORMAL if can_start else tk.DISABLED)
        self.pause_resume_button.config(state=tk.NORMAL if can_pause_resume else tk.DISABLED)
        self.cancel_button.config(state=tk.NORMAL if can_cancel else tk.DISABLED)
        if status == "Paused":
            self.pause_resume_button.config(text="Resume")
        else:
            self.pause_resume_button.config(text="Pause")

    def handle_progress_update(self, job_id, message_type, data):
        details = self.job_details.get(job_id)
        if not details:
            return
        try:
            item = self.tree.item(job_id)
        except tk.TclError:
            return
        if not item:
            return
        values = list(item["values"])
        pbar = details.get('pbar')
        terminal_job = False
        new_status_display = details.get('status', values[3])
        if message_type == 'status_update':
            new_status_display = data
            details['status'] = data
        elif message_type == 'total_size':
            details['total_size'] = data
            values[1] = f"{data / (1024*1024):.2f} MB"
            if pbar:
                pbar.config(maximum=data)
        elif message_type == 'initial_chunks':
            details['downloaded'] = data
            if pbar:
                pbar.config(value=data)
            if details['total_size'] > 0:
                percent = (details['downloaded'] / details['total_size']) * 100
                new_status_display = f"{percent:.1f}%"
                details['status'] = 'Downloading'
        elif message_type == 'reset_progress':
            details['downloaded'] = 0
            if pbar:
                pbar.config(value=0)
            new_status_display = '0.0%'
            details['status'] = 'Downloading'
        elif message_type == 'chunk_done':
            # only update progress if not paused
            if details['status'] != 'Paused':
                details['downloaded'] += data
                if pbar and details['total_size'] > 0:
                    pbar.config(value=details['downloaded'])
                    percent = (details['downloaded'] / details['total_size']) * 100
                    new_status_display = f"{percent:.1f}%"
        elif message_type == 'download_complete' or message_type == 'done':
            new_status_display = data if data else "Finished"
            details['status'] = 'Done'
            terminal_job = True
            if pbar:
                pbar.config(value=details.get('total_size', 0))
        elif message_type == 'error':
            new_status_display = data
            details['status'] = 'Error'
            terminal_job = True

        values[3] = new_status_display
        if self.tree.exists(job_id):
            self.tree.item(job_id, values=values)

        if pbar and self.tree.exists(job_id):
            bbox = self.tree.bbox(job_id, "progress")
            if bbox and len(bbox) == 4:
                if not pbar.winfo_exists():
                    pbar = ttk.Progressbar(self.tree, orient='horizontal', mode='determinate', style="TProgressbar")
                    details['pbar'] = pbar
                    if details.get('total_size', 0) > 0:
                        pbar.config(maximum=details['total_size'])
                    if details.get('downloaded', 0) > 0:
                        pbar.config(value=details['downloaded'])
                pbar.place(x=bbox[0], y=bbox[1], width=bbox[2], height=bbox[3])
            else:
                try:
                    if pbar.winfo_ismapped():
                        pbar.place_forget()
                except Exception:
                    pass

        if terminal_job:
            print(f"[{job_id}] งานจบ (สถานะ: {values[3]}), ทำความสะอาด Pbar...")
            if pbar:
                try:
                    pbar.destroy()
                except Exception:
                    pass
            if job_id in self.job_details:
                self.job_details[job_id]['pbar'] = None

        selected = self.tree.selection()
        if selected and selected[0] == job_id:
            self.update_button_states()

    def on_closing(self):
        print("GUI: กำลังปิด...")
        # stop clipboard monitor
        self.clipboard_stop.set()
        self.downloader.stop()
        self.destroy()

# -------------------------------------------------------------------
# ส่วนหลัก
# -------------------------------------------------------------------
if __name__ == "__main__":
    DOWNLOAD_EXTENSIONS = [".zip", ".rar", ".7z", ".exe", ".msi", ".mp4", ".mkv", ".avi", ".mp3", ".wav", ".pdf", ".iso", ".dat", ".img"]
    print(f"ระบบตรวจจับคลิปบอร์ดเริ่มทำงาน (v16 - Queue Engine, {MAX_CONCURRENT_JOBS} Concurrent)...")
    app = None
    try:
        app = MainApplication(DOWNLOAD_EXTENSIONS)
        app.mainloop()
    except KeyboardInterrupt:
        print("\nกำลังปิดโปรแกรม (KeyboardInterrupt)...")
        if app:
            app.on_closing()
    except Exception as e:
        print(f"เกิดข้อผิดพลาดกับ Application หลัก: {e}")
        import traceback; traceback.print_exc()
        if app:
            app.on_closing()
