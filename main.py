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
import uuid # For job IDs

import asyncio
import aiohttp
import aiofiles

import queue
import json
# Removed: import hashlib

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
    """ดาวน์โหลดไฟล์แบบ 1 เธรด (พื้นฐาน)"""
    if start_time is None:
        start_time = time.monotonic() #<- Correct indentation
    try:
        # Correct indentation for the 'with' block inside 'try'
        with requests.get(url, stream=True) as r:
            r.raise_for_status()
            with open(save_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk) #<- Correct indentation
        # Correct indentation for lines after the 'with' blocks but still in 'try'
        end_time = time.monotonic()
        duration = end_time - start_time
        print(f"ดาวน์โหลดพื้นฐานเสร็จสิ้น! เวลา: {duration:.2f} วินาที")
        # messagebox.showinfo(...) # Commented out

    except requests.exceptions.RequestException as e:
        # Correct indentation for the line inside 'except'
        print(f"เกิดข้อผิดพลาดในการดาวน์โหลดพื้นฐาน: {e}")

# -------------------------------------------------------------------
# เอนจิ้น Async หลัก (มี Pause/Resume, ไม่มี Verification)
# -------------------------------------------------------------------

async def get_file_info_async(session, url):
    """ตรวจสอบข้อมูลไฟล์ (ขนาด, รองรับ range)"""
    try:
        async with session.head(url, allow_redirects=True) as r:
            r.raise_for_status()
            file_size = r.headers.get('Content-Length')
            accept_ranges = r.headers.get('Accept-Ranges')
            if not file_size: return None, False
            file_size = int(file_size)
            supports_ranges = (accept_ranges == 'bytes')
            print(f"เอนจิ้น Async: ขนาดไฟล์: {file_size / (1024*1024):.2f} MB, รองรับ Ranges: {supports_ranges}")
            return file_size, supports_ranges
    except Exception as e:
        print(f"เกิดข้อผิดพลาดในการตรวจสอบข้อมูลไฟล์: {e}")
        return None, False

def load_progress(state_file_path):
    """โหลดสถานะการดาวน์โหลดจากไฟล์ .progress.json"""
    if not os.path.exists(state_file_path): return None
    try:
        with open(state_file_path, 'r') as f:
            state = json.load(f); state['completed_chunks'] = set(state['completed_chunks'])
            print(f"โหลด {len(state['completed_chunks'])} chunks ที่เสร็จแล้วจากไฟล์สถานะ.")
            return state
    except Exception as e:
        print(f"ไฟล์สถานะเสียหาย เริ่มใหม่. Error: {e}"); return None

async def save_progress_async(state_file_path, file_size, chunk_size, completed_chunks_set):
    """บันทึกสถานะการดาวน์โหลดลงไฟล์ .progress.json"""
    state = {'total_size': file_size, 'chunk_size': chunk_size, 'completed_chunks': list(completed_chunks_set)}
    try:
        async with aiofiles.open(state_file_path, 'w') as f: await f.write(json.dumps(state, indent=4))
    except Exception as e: print(f"เกิดข้อผิดพลาดในการบันทึกสถานะ: {e}")

async def state_saver(state_file_path, file_size, chunk_size, completed_chunks_set, pause_event: asyncio.Event):
    """Task ที่คอยบันทึกสถานะทุก interval หรือเมื่อ Pause"""
    while True:
        save_task = None
        pause_wait_task = None
        resume_waiter = None
        try:
            save_task = asyncio.create_task(asyncio.sleep(STATE_SAVE_INTERVAL))
            pause_wait_task = asyncio.create_task(pause_event.wait())

            done, pending = await asyncio.wait(
                [save_task, pause_wait_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass

            await save_progress_async(state_file_path, file_size, chunk_size, completed_chunks_set)
            print("[State Saver] บันทึกสถานะ...")

            if pause_wait_task in done and pause_event.is_set():
                print("[State Saver] Paused, waiting for resume...")
                resume_waiter = asyncio.create_task(pause_event.wait())
                await resume_waiter # This will wait indefinitely until pause_event.clear() is called elsewhere
                print("[State Saver] Resumed.")

        except asyncio.CancelledError:
            print("[State Saver] ถูกสั่งให้หยุด, บันทึกครั้งสุดท้าย...")
            await save_progress_async(state_file_path, file_size, chunk_size, completed_chunks_set); break
        except Exception as e:
            print(f"[State Saver] Error: {e}")
            if save_task and not save_task.done(): save_task.cancel()
            if pause_wait_task and not pause_wait_task.done(): pause_wait_task.cancel()
            if resume_waiter and not resume_waiter.done(): resume_waiter.cancel()
            await asyncio.sleep(STATE_SAVE_INTERVAL)

async def file_writer(save_path, write_queue, progress_queue, completed_chunks_set, chunk_size, job_id):
    """Task ที่คอยเขียนไฟล์ลงดิสก์"""
    async with aiofiles.open(save_path, 'r+b') as f:
        while True:
            try:
                start_byte, data = await write_queue.get()
                await f.seek(start_byte); await f.write(data)
                chunk_index = start_byte // chunk_size; completed_chunks_set.add(chunk_index)
                progress_queue.put_nowait((job_id, 'chunk_done', len(data)))
                write_queue.task_done()
            except asyncio.CancelledError: break
            except Exception as e: print(f"[Writer] Error: {e}"); write_queue.task_done()

async def worker(worker_id, session, url, download_queue, write_queue, download_failed_event, pause_event: asyncio.Event):
    """Task 'คนงาน' ดาวน์โหลด 1 คน (มีระบบ Pause)"""
    while True:
        try:
            if pause_event.is_set():
                print(f"[Worker {worker_id}] Paused, waiting...")
                await pause_event.wait()
                print(f"[Worker {worker_id}] Resumed.")

            start_byte, end_byte, retry_count = await download_queue.get()
            headers = {'Range': f'bytes={start_byte}-{end_byte}'}
            async with session.get(url, headers=headers) as r:
                r.raise_for_status(); data = await r.content.read()
                await write_queue.put((start_byte, data))
            download_queue.task_done()
        except asyncio.CancelledError: break
        except Exception as e:
            if retry_count < MAX_RETRIES:
                retry_count += 1; await asyncio.sleep(retry_count)
                await download_queue.put((start_byte, end_byte, retry_count))
            else: print(f"!!! Worker {worker_id} ล้มเหลวถาวร chunk เริ่มต้น {start_byte}"); download_failed_event.set()
            download_queue.task_done()

async def cancel_watcher(cancel_event: asyncio.Event):
    """Task ที่คอยดูธงยกเลิก"""
    try: await cancel_event.wait(); print("[Cancel Watcher] ตรวจพบการยกเลิก!")
    except asyncio.CancelledError: pass

async def download_dynamic_async(job_id, url, save_path, progress_queue, cancel_event: asyncio.Event, pause_event: asyncio.Event):
    """ฟังก์ชันจัดการดาวน์โหลดหลัก (มี Pause)"""
    start_time = time.monotonic()
    state_file_path = save_path + ".progress.json"
    all_tasks = []
    chunks_to_download = 0 # กำหนดค่าเริ่มต้นนอก try

    try:
        async with aiohttp.ClientSession() as session:
            loaded_state = load_progress(state_file_path); completed_chunks_set = set()
            if loaded_state:
                completed_chunks_set = loaded_state['completed_chunks']
                initial_downloaded_size = len(completed_chunks_set) * DEFAULT_CHUNK_SIZE
                progress_queue.put_nowait((job_id, 'initial_chunks', initial_downloaded_size))

            file_size, supports_ranges = await get_file_info_async(session, url)
            if file_size is None: raise Exception("ไม่สามารถรับข้อมูลไฟล์ได้")
            if not supports_ranges:
                if os.path.exists(state_file_path): os.remove(state_file_path)
                print("เซิร์ฟเวอร์ไม่รองรับ ranges, ใช้การดาวน์โหลดพื้นฐาน...")
                download_basic(url, save_path, start_time)
                progress_queue.put_nowait((job_id, 'done', f"เสร็จสิ้น ({time.monotonic() - start_time:.2f} วินาที)"))
                return

            if loaded_state and loaded_state['total_size'] != file_size:
                print("!!! ขนาดไฟล์บนเซิร์ฟเวอร์เปลี่ยน! เริ่มดาวน์โหลดใหม่."); completed_chunks_set.clear()
                if os.path.exists(state_file_path): os.remove(state_file_path)
                progress_queue.put_nowait((job_id, 'reset_progress', None))

            progress_queue.put_nowait((job_id, 'total_size', file_size))

            if not loaded_state or not os.path.exists(save_path):
                try:
                    async with aiofiles.open(save_path, 'wb') as f: await f.seek(file_size - 1); await f.write(b'\0')
                    print(f"[{job_id}] จองพื้นที่ไฟล์เรียบร้อย")
                except IOError as e: raise Exception(f"ไม่สามารถเขียนไฟล์ได้: {e}")
            else:
                 print(f"[{job_id}] ใช้ไฟล์ที่ดาวน์โหลดค้างไว้")

            download_queue = asyncio.Queue(); write_queue = asyncio.Queue(); download_failed_event = asyncio.Event()

            chunks_to_download = 0
            current_byte = 0
            while current_byte < file_size:
                chunk_index = current_byte // DEFAULT_CHUNK_SIZE
                if chunk_index not in completed_chunks_set:
                    start_byte = current_byte; end_byte = min(current_byte + DEFAULT_CHUNK_SIZE - 1, file_size - 1)
                    await download_queue.put((start_byte, end_byte, 0)); chunks_to_download += 1
                current_byte += DEFAULT_CHUNK_SIZE

            if chunks_to_download == 0:
                print(f"[{job_id}] ไฟล์ดาวน์โหลดเสร็จสมบูรณ์แล้ว (จากครั้งก่อน).")
            else:
                print(f"[{job_id}] ต้องดาวน์โหลดอีก {chunks_to_download} chunks.")
                writer_task = asyncio.create_task(
                    file_writer(save_path, write_queue, progress_queue, completed_chunks_set, DEFAULT_CHUNK_SIZE, job_id))
                workers = [asyncio.create_task(
                    worker(i + 1, session, url, download_queue, write_queue, download_failed_event, pause_event))
                    for i in range(CONCURRENT_WORKERS)]
                saver_task = asyncio.create_task(
                    state_saver(state_file_path, file_size, DEFAULT_CHUNK_SIZE, completed_chunks_set, pause_event))
                download_join_task = asyncio.create_task(download_queue.join())
                failed_event_task = asyncio.create_task(download_failed_event.wait())
                cancel_wait_task = asyncio.create_task(cancel_event.wait())

                all_tasks = workers + [writer_task, saver_task, download_join_task, failed_event_task, cancel_wait_task]

                print(f"[{job_id}] กำลังรอ ดาวน์โหลด/ล้มเหลว/ยกเลิก...")
                done, pending = await asyncio.wait(
                    [download_join_task, failed_event_task, cancel_wait_task],
                    return_when=asyncio.FIRST_COMPLETED)

                print(f"[{job_id}] กำลังหยุด workers และ watchers...")
                tasks_to_cancel_immediately = workers + list(pending)
                for t in tasks_to_cancel_immediately: t.cancel()
                await asyncio.gather(*tasks_to_cancel_immediately, return_exceptions=True)

                if cancel_wait_task in done:
                    print(f"[{job_id}] ดาวน์โหลดถูกยกเลิก! กำลังล้างข้อมูล...")
                    progress_queue.put_nowait((job_id, 'error', "Cancelled"))
                    writer_task.cancel(); saver_task.cancel()
                    await asyncio.gather(writer_task, saver_task, return_exceptions=True)
                    try:
                        if os.path.exists(save_path): os.remove(save_path)
                        if os.path.exists(state_file_path): os.remove(state_file_path)
                    except Exception as e: print(f"[{job_id}] เกิดข้อผิดพลาดขณะล้างข้อมูล: {e}")
                    return

                elif failed_event_task in done:
                    print(f"[{job_id}] ดาวน์โหลดล้มเหลวถาวร!")
                    progress_queue.put_nowait((job_id, 'error', "Download Failed"))
                    writer_task.cancel(); saver_task.cancel()
                    await asyncio.gather(writer_task, saver_task, return_exceptions=True)
                    return

                else: # download_join_task เสร็จสิ้น
                    print(f"[{job_id}] ดาวน์โหลด chunks ทั้งหมดเสร็จสิ้น.")
                    print(f"[{job_id}] กำลังรอเขียนไฟล์ให้เสร็จ...")
                    await write_queue.join()
                    print(f"[{job_id}] เขียนไฟล์เสร็จสมบูรณ์.")
                    writer_task.cancel(); saver_task.cancel()
                    await asyncio.gather(writer_task, saver_task, return_exceptions=True)

            print(f"[{job_id}] กระบวนการดาวน์โหลดเสร็จสิ้น.")
            if os.path.exists(state_file_path):
                 try: os.remove(state_file_path)
                 except Exception as e: print(f"[{job_id}] เกิดข้อผิดพลาดในการลบไฟล์สถานะ: {e}")

            end_time = time.monotonic()
            duration = end_time - start_time
            progress_queue.put_nowait((job_id, 'done', f"เสร็จสิ้น ({duration:.2f} วินาที)"))

    except Exception as e:
        print(f"!!! [{job_id}] ตัวจัดการดาวน์โหลดล้มเหลว: {e}")
        progress_queue.put_nowait((job_id, 'error', f"Manager Error: {type(e).__name__}"))
    finally:
        # Correct indentation for the 'pass' statement
        print(f"[{job_id}] Entering finally block, cancelling remaining tasks...")
        for task in all_tasks:
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        print(f"[{job_id}] Exiting finally block.")
        pass #<--- Correct indentation

# -------------------------------------------------------------------
# คลาส Application หลัก
# -------------------------------------------------------------------
class ClipboardMonitor(threading.Thread):
    def __init__(self, new_url_queue, download_extensions): super().__init__(daemon=True); self.new_url_queue = new_url_queue; self.download_extensions = download_extensions; self.last_copied = ""
    def run(self):
        print("[Clipboard Monitor] เริ่มทำงาน.")
        while True:
            try:
                copied_text = pyperclip.paste()
                if copied_text != self.last_copied:
                    self.last_copied = copied_text; text_to_check = copied_text.lower().strip()
                    if text_to_check.startswith(("http://", "https://")):
                        is_dl = any(text_to_check.split('?')[0].endswith(ext) for ext in self.download_extensions)
                        if is_dl: self.new_url_queue.put(copied_text)
                time.sleep(1)
            except Exception as e: print(f"[Clipboard Monitor] Error: {e}"); time.sleep(5)

class DownloadManager:
    # (อัปเกรด! เฟส 16) เพิ่ม Semaphore
    def __init__(self, progress_queue):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.run_loop, daemon=True)
        self.progress_queue = progress_queue
        self.active_jobs = {} # { job_id: {'future': Future, 'cancel': asyncio.Event, 'pause': asyncio.Event} }
        # (ใหม่!) สร้าง Semaphore
        self.download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

    def run_loop(self): asyncio.set_event_loop(self.loop); self.loop.run_forever()
    def start(self): self.thread.start()
    def stop(self):
        print("[Async Engine] กำลังหยุด..."); jobs_to_stop = list(self.active_jobs.values())
        for job_data in jobs_to_stop:
            if job_data['future'] and not job_data['future'].done():
                # We need to ensure the semaphore is released if a task is cancelled
                # This might require more complex cancellation handling within the wrapper
                self.loop.call_soon_threadsafe(job_data['cancel'].set)
        time.sleep(1); self.loop.call_soon_threadsafe(self.loop.stop); self.thread.join(timeout=5); print("[Async Engine] หยุดแล้ว.")

    # (อัปเกรด! เฟส 16) submit_job ใช้ Semaphore
    def submit_job(self, job_id, url, save_path):
        print(f"[Async Engine] กำลังส่งงาน: {job_id}")
        cancel_event = asyncio.Event()
        pause_event = asyncio.Event()

        # (อัปเกรด!) Wrapper รอ Semaphore
        async def _job_wrapper():
            acquired_semaphore = False # Track if semaphore was acquired
            try:
                # แจ้ง GUI ว่ากำลังรอคิว (ถ้ายังไม่เริ่ม)
                self.progress_queue.put_nowait((job_id, 'status_update', 'Waiting...'))
                print(f"[{job_id}] Waiting for semaphore...")
                # >> รอ Semaphore <<
                async with self.download_semaphore:
                    acquired_semaphore = True
                    print(f"[{job_id}] Acquired semaphore. Task starting...")
                    # แจ้ง GUI ว่าเริ่มโหลดแล้ว
                    self.progress_queue.put_nowait((job_id, 'status_update', 'Starting...'))

                    # >> เรียกใช้เอนจิ้นหลัก <<
                    await download_dynamic_async(job_id, url, save_path, self.progress_queue, cancel_event, pause_event)

                # Semaphore ถูกคืนอัตโนมัติเมื่อออกจาก 'async with'
                print(f"[{job_id}] Released semaphore. Task wrapper finished.")

            except asyncio.CancelledError:
                print(f"[{job_id}] Task ถูกยกเลิก (wrapper)")
                # ไม่ต้องแจ้ง GUI error เพราะ Cancelled คือปกติ
            except Exception as e:
                print(f"!!! [{job_id}] Task ล้มเหลว (wrapper): {e}")
                self.progress_queue.put_nowait((job_id, 'error', f"Engine Error: {type(e).__name__}"))
            finally:
                # Ensure semaphore is released if cancelled *after* acquiring it
                # The `async with` handles this automatically unless cancelled harshly.
                # However, explicit check might be needed in complex scenarios.
                if acquired_semaphore:
                     print(f"[{job_id}] Semaphore context exited.")
                     pass # Semaphore is released by 'async with' context exit
                else:
                     print(f"[{job_id}] Wrapper finished without acquiring semaphore (likely cancelled early).")


        # (สำคัญ!) สร้าง Task แต่ "ไม่ await" - ให้ Semaphore จัดการ
        task = asyncio.run_coroutine_threadsafe( _job_wrapper(), self.loop).result() # Get the task object

        self.active_jobs[job_id] = {'future': task, 'cancel': cancel_event, 'pause': pause_event}
        # Don't add done callback here, let the wrapper handle final state
        print(f"[Async Engine] งาน {job_id} ถูกเพิ่มเข้าคิว (รอ Semaphore)")

    def cancel_job(self, job_id):
         if job_id in self.active_jobs:
             print(f"[Async Engine] กำลังยกเลิกงาน: {job_id}")
             job_data = self.active_jobs[job_id]
             if job_data['pause'].is_set(): self.loop.call_soon_threadsafe(job_data['pause'].clear)
             self.loop.call_soon_threadsafe(job_data['cancel'].set)
         else: print(f"[Async Engine] ไม่พบงาน {job_id} สำหรับการยกเลิก.")

    def pause_job(self, job_id):
        if job_id in self.active_jobs:
            print(f"[Async Engine] กำลังหยุดงาน: {job_id}")
            job_data = self.active_jobs[job_id]
            self.loop.call_soon_threadsafe(job_data['pause'].set)
        else: print(f"[Async Engine] ไม่พบงาน {job_id} สำหรับการหยุด.")

    def resume_job(self, job_id):
        if job_id in self.active_jobs:
            print(f"[Async Engine] กำลังทำต่อ: {job_id}")
            job_data = self.active_jobs[job_id]
            self.loop.call_soon_threadsafe(job_data['pause'].clear)
        else: print(f"[Async Engine] ไม่พบงาน {job_id} สำหรับการทำต่อ.")

class MainApplication(tk.Tk):
    def __init__(self, download_extensions):
        super().__init__()
        self.title(f"Auto Queue Downloader (v16 - {MAX_CONCURRENT_JOBS} Concurrent)") # (เปลี่ยนชื่อ)
        self.geometry("700x450")

        self.new_url_queue = queue.Queue(); self.progress_queue = queue.Queue()
        self.job_details = {}

        self.downloader = DownloadManager(self.progress_queue)
        self.monitor = None # Will be created after GUI

        self.create_widgets()

        self.downloader.start()
        self.check_queues()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        # Create and start monitor
        self.monitor = ClipboardMonitor(self.new_url_queue, download_extensions)
        self.monitor.start()

    def create_widgets(self):
        # --- Top Frame (Add URL) ---
        add_frame = ttk.Frame(self, padding="10"); add_frame.pack(fill=tk.X)
        ttk.Label(add_frame, text="URL:").pack(side=tk.LEFT, padx=(0, 5))
        self.manual_url_entry = ttk.Entry(add_frame); self.manual_url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.add_button = ttk.Button(add_frame, text="Add Download", command=self.add_manual_download); self.add_button.pack(side=tk.LEFT)

        # --- Treeview Frame ---
        tree_frame = ttk.Frame(self, padding="10"); tree_frame.pack(fill=tk.BOTH, expand=True)
        columns = ("filename", "size", "progress", "status"); self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        self.tree.heading("filename", text="File Name"); self.tree.heading("size", text="Size")
        self.tree.heading("progress", text="Progress"); self.tree.heading("status", text="Status")
        self.tree.column("filename", width=300); self.tree.column("size", width=80, anchor=tk.E)
        self.tree.column("progress", width=150); self.tree.column("status", width=100, anchor=tk.W)
        s = ttk.Style(); s.theme_use('vista')
        s.layout("TProgressbar",[('TProgressbar.trough', {'children': [('TProgressbar.pbar', {'side': 'left', 'sticky': 'ns'})], 'sticky': 'nswe'})])
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview); self.tree.configure(yscroll=scrollbar.set); scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self.update_button_states) # Moved bind here

        # --- Control Frame ---
        control_frame = ttk.Frame(self, padding="10"); control_frame.pack(fill=tk.X)
        self.pause_resume_button = ttk.Button(control_frame, text="Pause", command=self.toggle_pause_resume, state=tk.DISABLED)
        self.pause_resume_button.pack(side=tk.LEFT, padx=5)
        self.start_button = ttk.Button(control_frame, text="Start/Restart", command=self.start_selected_download, state=tk.DISABLED) # Start disabled
        self.start_button.pack(side=tk.LEFT, padx=5)
        self.cancel_button = ttk.Button(control_frame, text="Cancel/Delete", command=self.cancel_selected_download, state=tk.DISABLED) # Start disabled
        self.cancel_button.pack(side=tk.LEFT, padx=5)
        # Initial button state update
        self.update_button_states()


    def check_queues(self):
        try:
            while not self.new_url_queue.empty(): url = self.new_url_queue.get_nowait(); self.add_to_queue(url)
        except queue.Empty: pass
        try:
            while not self.progress_queue.empty(): job_id, message_type, data = self.progress_queue.get_nowait(); self.handle_progress_update(job_id, message_type, data)
        except queue.Empty: pass
        self.after(100, self.check_queues)

    def add_manual_download(self):
        url = self.manual_url_entry.get();
        if url: self.add_to_queue(url); self.manual_url_entry.delete(0, tk.END)

    def add_to_queue(self, url):
        filename = url.split('/')[-1].split('?')[0] or "unknown_file"; job_id = str(uuid.uuid4())
        self.job_details[job_id] = {'url': url, 'save_path': None, 'total_size': 0, 'downloaded': 0, 'pbar': None, 'status': 'Queued'}
        item_id = self.tree.insert("", tk.END, iid=job_id, values=(filename,"?",0,"Queued"))
        pbar = ttk.Progressbar(self.tree, orient='horizontal', mode='determinate', style="TProgressbar")
        self.job_details[job_id]['pbar'] = pbar
        print(f"GUI: เพิ่ม {filename} (Job ID: {job_id}) เข้าคิว")
        # Don't update buttons here, wait for selection

    def start_selected_download(self):
        # (ปรับปรุง!) เช็คสถานะ Queued หรือ Error ก่อนเริ่ม
        selected_items = self.tree.selection()
        if not selected_items: messagebox.showwarning("คำเตือน", "กรุณาเลือกไฟล์"); return
        job_id = selected_items[0]
        if job_id not in self.job_details: return
        details = self.job_details[job_id]; status = details['status']

        # อนุญาตให้เริ่ม/เริ่มใหม่ เฉพาะสถานะเหล่านี้
        if status not in ("Queued", "Error", "Cancelled", "Download Failed", "Manager Error", "เก็บไฟล์ไว้"):
             messagebox.showwarning("คำเตือน", f"ไม่สามารถเริ่มงานในสถานะ '{status}' ได้"); return

        url = details['url']; filename = self.tree.item(job_id, "values")[0]
        initial_file = details.get('save_path') or filename
        save_path = filedialog.asksaveasfilename(initialfile=initial_file, title="เลือกที่บันทึกไฟล์")
        if not save_path: return

        details['save_path'] = save_path; details['downloaded'] = 0; details['status'] = 'Waiting...' # (อัปเกรด!) เปลี่ยนเป็น Waiting...

        self.tree.item(job_id, values=(filename, "?", 0, "Waiting...")) # แสดง Waiting...
        if details['pbar']:
             details['pbar'].config(value=0)
             # Defer placement

        print(f"GUI: กำลังส่ง {job_id} ไปรอ Semaphore ด้วย URL: {url}")
        self.downloader.submit_job(job_id, url, save_path) # ส่งงานเฉยๆ
        self.update_button_states()

    def cancel_selected_download(self):
        # (ปรับปรุง!) เช็คให้ดีก่อนลบ details
        selected_items = self.tree.selection();
        if not selected_items: return
        job_id = selected_items[0]
        details = self.job_details.get(job_id)
        if not details: return

        print(f"GUI: Requesting cancel for {job_id}")
        self.downloader.cancel_job(job_id)
        # ลบออกจาก GUI และ details ทันที
        if self.tree.exists(job_id): self.tree.delete(job_id)
        self.job_details.pop(job_id, None)
        print(f"GUI: ลบ {job_id} ออกจากคิว")
        self.update_button_states()

    def toggle_pause_resume(self):
        selected_items = self.tree.selection()
        if not selected_items: return
        job_id = selected_items[0]
        if job_id not in self.job_details: return
        details = self.job_details[job_id]; status = details['status']

        if status == 'Downloading':
            self.downloader.pause_job(job_id)
            details['status'] = 'Paused'
            if self.tree.exists(job_id): self.tree.item(job_id, values=(*self.tree.item(job_id, "values")[:3], "Paused"))
            print(f"GUI: สั่งหยุด {job_id}")
        elif status == 'Paused':
            self.downloader.resume_job(job_id)
            details['status'] = 'Downloading' # Internal status change
            # Let handle_progress_update change the display status
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
        # Check if job still exists (might be cancelled/deleted quickly)
        details = self.job_details.get(job_id)
        if not details:
             self.start_button.config(state=tk.DISABLED)
             self.pause_resume_button.config(state=tk.DISABLED, text="Pause")
             self.cancel_button.config(state=tk.DISABLED)
             return

        status = details.get('status', 'Unknown')

        can_start = status in ("Queued", "Error", "Cancelled", "Download Failed", "Manager Error", "เก็บไฟล์ไว้")
        can_pause_resume = status in ("Downloading", "Paused")
        can_cancel = status not in ("Done", "เสร็จสิ้น", "ลบแล้ว") # Almost always cancelable unless truly finished/deleted

        self.start_button.config(state=tk.NORMAL if can_start else tk.DISABLED)
        self.pause_resume_button.config(state=tk.NORMAL if can_pause_resume else tk.DISABLED)
        self.cancel_button.config(state=tk.NORMAL if can_cancel else tk.DISABLED)

        if status == "Paused":
            self.pause_resume_button.config(text="Resume")
        else:
            self.pause_resume_button.config(text="Pause")

    def handle_progress_update(self, job_id, message_type, data):
        # (ปรับปรุง!) เพิ่ม status_update
        details = self.job_details.get(job_id)
        if not details: return
        try: item = self.tree.item(job_id);
        except tk.TclError: return
        if not item: return

        values = list(item["values"]); pbar = details.get('pbar')
        terminal_job = False
        new_status_display = details.get('status', values[3]) # ใช้ status ภายในเป็นหลัก

        # (ใหม่!) จัดการ status update จาก engine
        if message_type == 'status_update':
             new_status_display = data # เช่น "Waiting...", "Starting..."
             details['status'] = data # อัปเดต status ภายในด้วย (ถ้าจำเป็น)

        elif message_type == 'total_size':
            details['total_size'] = data; values[1] = f"{data / (1024*1024):.2f} MB"
            if pbar: pbar.config(maximum=data)
        elif message_type == 'initial_chunks':
            details['downloaded'] = data
            if pbar: pbar.config(value=data)
            if details['total_size'] > 0:
                percent = (details['downloaded'] / details['total_size']) * 100
                new_status_display = f"{percent:.1f}%"
                details['status'] = 'Downloading'
        elif message_type == 'reset_progress':
            details['downloaded'] = 0
            if pbar: pbar.config(value=0)
            new_status_display = '0.0%'
            details['status'] = 'Downloading'
        elif message_type == 'chunk_done':
             if details['status'] != 'Paused': # เช็ค Pause สำคัญมาก
                details['downloaded'] += data
                if pbar and details['total_size'] > 0:
                    pbar.config(value=details['downloaded'])
                    percent = (details['downloaded'] / details['total_size']) * 100
                    new_status_display = f"{percent:.1f}%"
                    # ไม่ต้องตั้ง details['status'] = 'Downloading' ซ้ำๆ
             # ถ้า Pause อยู่ ปล่อย new_status_display เป็น "Paused"

        elif message_type == 'download_complete' or message_type == 'done':
            new_status_display = data if data else "Finished"
            details['status'] = 'Done'
            terminal_job = True
            if pbar: pbar.config(value=details.get('total_size', 0))

        elif message_type == 'error':
            new_status_display = data
            details['status'] = 'Error'
            terminal_job = True

        values[3] = new_status_display

        if self.tree.exists(job_id):
            self.tree.item(job_id, values=values)

        if pbar and self.tree.exists(job_id):
            # วาง Pbar (สำคัญมาก ต้องทำทุกครั้งที่มีอัปเดต)
            bbox = self.tree.bbox(job_id, "progress")
            if bbox and len(bbox) == 4:
                # ทำให้แน่ใจว่า pbar ถูกสร้างและเชื่อมโยงกับ tree ก่อน place
                if not pbar.winfo_exists():
                    pbar = ttk.Progressbar(self.tree, orient='horizontal', mode='determinate', style="TProgressbar")
                    details['pbar'] = pbar # อัปเดต reference เผื่อสร้างใหม่
                    # ตั้งค่า max/value ใหม่ ถ้ามีข้อมูลแล้ว
                    if details.get('total_size', 0) > 0: pbar.config(maximum=details['total_size'])
                    if details.get('downloaded', 0) > 0: pbar.config(value=details['downloaded'])

                pbar.place(x=bbox[0], y=bbox[1], width=bbox[2], height=bbox[3])
            elif pbar.winfo_ismapped(): # ถ้ามองไม่เห็น ให้ซ่อน
                 pbar.place_forget()

        if terminal_job:
            print(f"[{job_id}] งานจบ (สถานะ: {values[3]}), ทำความสะอาด Pbar...")
            if pbar: pbar.destroy()
            if job_id in self.job_details:
                 self.job_details[job_id]['pbar'] = None

        # อัปเดตสถานะปุ่มเมื่อ GUI อัปเดต
        selected = self.tree.selection()
        if selected and selected[0] == job_id:
            self.update_button_states()

    def on_closing(self):
        print("GUI: กำลังปิด..."); self.downloader.stop(); self.destroy()

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
        if app: app.on_closing()
    except Exception as e:
        print(f"เกิดข้อผิดพลาดกับ Application หลัก: {e}")
        import traceback; traceback.print_exc()
        if app: app.on_closing()