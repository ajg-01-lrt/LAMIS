"""AI Documentation Assistant frame.

Lets a technician ask natural-language questions against the ingested vendor
documentation and get a cited, grounded answer (RAG Phase 1). The model only
sees the excerpts retrieved from the local index, so it answers from the docs
or says it can't — it never invents commands or procedures.

Threading model: a single long-lived worker thread owns the DocAssistant (and
thus the SQLite-backed index — SQLite objects can't cross threads). It builds
the assistant once on first question (loading the embedding matrix a single
time) and then serves questions pulled off a queue. UI updates are marshaled
back to the Tk thread via ``controller.root.after`` — the same pattern as
NetworkAuditFrame.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext

import config


class AIAssistantFrame(ttk.Frame):
    """Tk frame for cited Q&A over the vendor doc index."""

    def __init__(self, parent: ttk.Frame, controller) -> None:
        super().__init__(parent)
        self.controller = controller
        self._assistant = None              # built lazily inside the worker
        # Each request is (text, platform_filter | None, image_bytes | None).
        self._requests: "queue.Queue[tuple]" = queue.Queue()
        self._worker_started = False
        self._pending_image = None          # PNG bytes of a pasted screenshot
        self._build()

    # -- UI construction -------------------------------------------------

    def _build(self) -> None:
        ask_box = ttk.LabelFrame(self, text="Ask the Documentation")
        ask_box.pack(fill=tk.X, pady=5)

        tk.Label(
            ask_box,
            text="Ask a question, paste an alarm / error to interpret, or paste "
                 "a screenshot (Ctrl+V) of an alarm list:",
            anchor="w",
        ).pack(fill=tk.X, padx=5, pady=(5, 2))

        self.question_text = tk.Text(ask_box, height=4, wrap=tk.WORD)
        self.question_text.pack(fill=tk.X, padx=5, pady=2)
        # Ctrl+Enter submits, matching the Ask button.
        self.question_text.bind("<Control-Return>", lambda _e: (self.ask(), "break")[1])
        # Ctrl+V grabs a screenshot off the clipboard if one is present; falls
        # through to normal text paste otherwise. Bind both cases (some layouts
        # deliver <Control-V>).
        self.question_text.bind("<Control-v>", self._on_paste)
        self.question_text.bind("<Control-V>", self._on_paste)

        controls = ttk.Frame(ask_box)
        controls.pack(fill=tk.X, padx=5, pady=5)
        # Platform scope — pins retrieval to one platform's docs (alarm codes
        # aren't unique across platforms, so this disambiguates).
        from utils.ai.assistant import PLATFORM_CHOICES
        self._platform_choices = PLATFORM_CHOICES
        tk.Label(controls, text="Platform:").pack(side=tk.LEFT)
        self.platform_var = tk.StringVar(value=PLATFORM_CHOICES[0][0])
        self.platform_combo = ttk.Combobox(
            controls, textvariable=self.platform_var, state="readonly", width=20,
            values=[label for label, _ in PLATFORM_CHOICES])
        self.platform_combo.pack(side=tk.LEFT, padx=(4, 12))
        self.ask_button = tk.Button(controls, text="Ask / Interpret", command=self.ask)
        self.ask_button.pack(side=tk.RIGHT)
        tk.Label(
            controls, text="(Ctrl+Enter)", fg="gray",
        ).pack(side=tk.RIGHT, padx=8)
        self.status_label = tk.Label(controls, text="Status: Ready", anchor="w", fg="gray")
        self.status_label.pack(side=tk.LEFT, padx=(12, 0))

        answer_box = ttk.LabelFrame(self, text="Answer")
        answer_box.pack(fill=tk.BOTH, expand=True, pady=5)
        self.answer_text = scrolledtext.ScrolledText(answer_box, height=14, wrap=tk.WORD)
        self.answer_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.answer_text.configure(state=tk.DISABLED)

        self._set_answer(
            "Answers are drawn only from the ingested vendor documentation, "
            "with [n] markers tying each statement to the numbered sources "
            "listed below it.\n\n"
            "• Ask a question (\"how do I change the management IP on SAOS 6?\")\n"
            "• Or paste alarm / error output — it identifies each alarm and gives "
            "the cause, impact, and clearing procedure.\n"
            "• Or paste a screenshot (Ctrl+V) of an alarm list — it reads the "
            "alarms off the image and interprets them the same way.\n\n"
            "Pick the platform for best results. If the docs don't cover "
            "something, the assistant says so rather than guess."
        )

    # -- actions ---------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable the assistant. When disabled (no valid API key),
        the input is locked and a message points to Help → Set OpenAI API Key.
        The controller also grays out the tab's radio button."""
        state = tk.NORMAL if enabled else tk.DISABLED
        self.ask_button.config(state=state)
        self.question_text.config(state=state)
        self.platform_combo.config(state=("readonly" if enabled else tk.DISABLED))
        if enabled:
            self.status_label.config(text="Status: Ready")
        else:
            self.status_label.config(text="Status: disabled — no valid API key")
            self._set_answer(
                "Doc Search is disabled because no valid OpenAI API key is set."
                "\n\nUse  Help → Set OpenAI API Key…  to enter one. The key is "
                "stored in the Windows Credential Manager for your user account "
                "— never in the ATLAS program files."
            )

    def _on_paste(self, _event):
        """Ctrl+V: if the clipboard holds an image, attach it as a screenshot to
        interpret and swallow the event; otherwise let normal text paste run."""
        image = self._grab_clipboard_image()
        if image is None:
            return None  # not an image -> default paste proceeds
        self._pending_image = image
        self.question_text.delete("1.0", tk.END)
        self.question_text.insert(
            tk.END, "[Screenshot attached — click \"Ask / Interpret\" to read "
                    "the alarms from it.]")
        self.status_label.config(text="Status: Screenshot attached")
        return "break"  # don't also paste bitmap garbage into the text box

    @staticmethod
    def _grab_clipboard_image():
        """Return PNG bytes of a clipboard image (or None). Handles both a raw
        bitmap and a copied image file. Large shots are downscaled to keep the
        vision call cheap; small alarm text stays legible at 1600px."""
        try:
            import io
            from PIL import Image, ImageGrab
        except Exception:
            return None
        try:
            grabbed = ImageGrab.grabclipboard()
        except Exception:
            return None
        img = None
        if isinstance(grabbed, list):  # a copied file, not a bitmap
            for path in grabbed:
                if str(path).lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".gif")):
                    try:
                        img = Image.open(path)
                        break
                    except Exception:
                        img = None
        elif grabbed is not None and hasattr(grabbed, "size"):
            img = grabbed
        if img is None:
            return None
        img = img.convert("RGB")
        longest = max(img.size)
        if longest > 1600:
            scale = 1600 / longest
            img = img.resize((max(1, int(img.size[0] * scale)),
                              max(1, int(img.size[1] * scale))))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def ask(self) -> None:
        image = self._pending_image
        text = self.question_text.get("1.0", tk.END).strip()
        if not text and image is None:
            return
        # Resolve the selected platform label to its doc-filter (None = Any).
        label = self.platform_var.get()
        platform_filter = next(
            (filt for lbl, filt in self._platform_choices if lbl == label), None)
        self.ask_button.config(state=tk.DISABLED)
        self.status_label.config(
            text="Status: Reading screenshot…" if image is not None
            else "Status: Searching docs…")
        self._pending_image = None  # consumed
        self._ensure_worker()
        self._requests.put((text, platform_filter, image))

    def _ensure_worker(self) -> None:
        if self._worker_started:
            return
        thread = threading.Thread(target=self._worker_loop, daemon=True)
        thread.start()
        self._worker_started = True

    def _worker_loop(self) -> None:
        """Owns the DocAssistant for the life of the frame. One question at a
        time (the Ask button is disabled while a query is in flight)."""
        while True:
            text, platform_filter, image = self._requests.get()
            try:
                if self._assistant is None:
                    self._post_status("Status: Loading document index…")
                    from utils.ai.assistant import DocAssistant
                    self._assistant = DocAssistant()
                if image is not None:
                    # Read the alarms off the screenshot, then interpret them
                    # against the docs (same grounding + safety nets as text).
                    answer = self._assistant.interpret_image(
                        image, platform_filter=platform_filter)
                else:
                    # answer() auto-routes: interpret pasted alarms/errors, else Q&A.
                    answer = self._assistant.answer(
                        text, platform_filter=platform_filter)
                self._post_result(text, answer)
            except Exception as exc:  # surface, don't crash the worker
                self._post_error(exc)

    # -- thread -> UI marshaling ----------------------------------------

    def _post_status(self, text: str) -> None:
        self.controller.root.after(0, lambda: self.status_label.config(text=text))

    def _post_result(self, question: str, answer) -> None:
        def _do() -> None:
            # render_answer appends the VERBATIM documentation for the cited
            # excerpts — in extractive mode the tech copies commands from there,
            # never from the model's prose.
            from utils.ai.assistant import render_answer
            self._set_answer(render_answer(answer))
            self.ask_button.config(state=tk.NORMAL)
            self.status_label.config(text="Status: Ready")
        self.controller.root.after(0, _do)

    def _post_error(self, exc: Exception) -> None:
        def _do() -> None:
            self._set_answer(
                f"Could not get an answer:\n\n{exc}\n\n"
                "Common causes: OPENAI_API_KEY not set in the environment, "
                "no API credit on the account, or no docs ingested yet."
            )
            self.ask_button.config(state=tk.NORMAL)
            self.status_label.config(text="Status: Error")
        self.controller.root.after(0, _do)

    def _set_answer(self, text: str) -> None:
        self.answer_text.configure(state=tk.NORMAL)
        self.answer_text.delete("1.0", tk.END)
        self.answer_text.insert(tk.END, text)
        self.answer_text.configure(state=tk.DISABLED)
