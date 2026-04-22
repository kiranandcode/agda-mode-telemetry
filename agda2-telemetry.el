;;; agda2-telemetry.el --- Action telemetry for Agda mode -*- lexical-binding: t; -*-

;; Records every user action, Agda response, and buffer state in a
;; SQLite database stored alongside the Agda file being edited.
;; Requires Emacs 29+ (built-in SQLite).

(require 'cl-lib)

;;; --- Variables ---

(defvar-local agda2-telemetry--db nil
  "SQLite database handle for the current buffer.")

(defvar-local agda2-telemetry--session-id nil
  "Unique session identifier for the current editing session.")

(defvar agda2-telemetry--advised nil
  "Non-nil once command advice has been installed.")

(defvar-local agda2-telemetry--last-event-id nil
  "Event ID returned by the most recent command log insertion.")

(defvar-local agda2-telemetry--current-event-id nil
  "Event ID for the interaction round currently in progress.")

(defvar-local agda2-telemetry--pre-buffer nil
  "Buffer text captured before the current interaction round.")

(defvar-local agda2-telemetry--pending-responses nil
  "List of (function-name . args-text) from Agda responses in this round.")

;;; --- Database ---

(defun agda2-telemetry--db-path ()
  "Return the path for the telemetry DB: same directory as the current file."
  (when buffer-file-name
    (expand-file-name ".agda-telemetry.db"
                      (file-name-directory buffer-file-name))))

(defun agda2-telemetry--ensure-db ()
  "Open (or create) the telemetry database for the current buffer."
  (when (and (not agda2-telemetry--db) buffer-file-name)
    (let ((db-path (agda2-telemetry--db-path)))
      (setq agda2-telemetry--db (sqlite-open db-path))
      (sqlite-execute agda2-telemetry--db "PRAGMA journal_mode=WAL")
      (sqlite-execute agda2-telemetry--db
                      "CREATE TABLE IF NOT EXISTS events (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         session_id TEXT NOT NULL,
                         timestamp TEXT NOT NULL,
                         command TEXT NOT NULL,
                         file TEXT NOT NULL,
                         point INTEGER,
                         line INTEGER,
                         col INTEGER,
                         goal_number INTEGER,
                         goal_content TEXT,
                         buffer_modified INTEGER,
                         extra TEXT
                       )")
      (sqlite-execute agda2-telemetry--db
                      "CREATE TABLE IF NOT EXISTS snapshots (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         event_id INTEGER NOT NULL,
                         content TEXT NOT NULL
                       )")
      (sqlite-execute agda2-telemetry--db
                      "CREATE TABLE IF NOT EXISTS responses (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         event_id INTEGER NOT NULL,
                         sequence INTEGER NOT NULL,
                         function_name TEXT NOT NULL,
                         args_text TEXT
                       )")
      (sqlite-execute agda2-telemetry--db
                      "CREATE INDEX IF NOT EXISTS idx_events_session
                       ON events (session_id)")
      (sqlite-execute agda2-telemetry--db
                      "CREATE INDEX IF NOT EXISTS idx_events_timestamp
                       ON events (timestamp)")
      (sqlite-execute agda2-telemetry--db
                      "CREATE INDEX IF NOT EXISTS idx_snapshots_event
                       ON snapshots (event_id)")
      (sqlite-execute agda2-telemetry--db
                      "CREATE INDEX IF NOT EXISTS idx_responses_event
                       ON responses (event_id)"))))

(defun agda2-telemetry--close-db ()
  "Log session-end and close the telemetry database for the current buffer."
  (when agda2-telemetry--db
    (condition-case nil
        (progn
          (agda2-telemetry--log 'session-end)
          (when agda2-telemetry--last-event-id
            (sqlite-execute
             agda2-telemetry--db
             "INSERT INTO snapshots (event_id, content) VALUES (?, ?)"
             (list agda2-telemetry--last-event-id (buffer-string)))))
      (error nil))
    (ignore-errors (sqlite-close agda2-telemetry--db))
    (setq agda2-telemetry--db nil)))

;;; --- Helpers ---

(defun agda2-telemetry--goal-info ()
  "Return (goal-number . goal-content) if point is in a goal, else nil."
  (when (fboundp 'agda2-goal-at)
    (let ((info (ignore-errors (agda2-goal-at (point)))))
      (when info
        (let* ((o (car info))
               (g (cadr info))
               (txt (ignore-errors
                      (buffer-substring-no-properties
                       (+ (overlay-start o) 2)
                       (- (overlay-end o) 2)))))
          (cons g txt))))))

(defun agda2-telemetry--compute-diff (before after)
  "Compute unified diff between BEFORE and AFTER strings.
Returns the diff text, or nil if identical."
  (when (not (string= before after))
    (let ((file-a (make-temp-file "agda-tel-a"))
          (file-b (make-temp-file "agda-tel-b")))
      (unwind-protect
          (progn
            (with-temp-buffer
              (insert before)
              (write-region (point-min) (point-max) file-a nil 'silent))
            (with-temp-buffer
              (insert after)
              (write-region (point-min) (point-max) file-b nil 'silent))
            (with-temp-buffer
              (call-process "diff" nil t nil "-u" file-a file-b)
              (buffer-string)))
        (delete-file file-a nil)
        (delete-file file-b nil)))))

;;; --- Event Logging ---

(defun agda2-telemetry--log (command-name)
  "Record COMMAND-NAME to the telemetry database.  Sets `agda2-telemetry--last-event-id'."
  (condition-case nil
      (when (and (derived-mode-p 'agda2-mode)
                 buffer-file-name
                 agda2-telemetry--db)
        (let* ((goal-info (agda2-telemetry--goal-info))
               (goal-num (car goal-info))
               (goal-txt (cdr goal-info))
               (line (line-number-at-pos))
               (col (current-column)))
          (sqlite-execute
           agda2-telemetry--db
           "INSERT INTO events
              (session_id, timestamp, command, file, point,
               line, col, goal_number, goal_content, buffer_modified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
           (list agda2-telemetry--session-id
                 (format-time-string "%Y-%m-%dT%H:%M:%S.%3N%z")
                 (symbol-name command-name)
                 (file-name-nondirectory buffer-file-name)
                 (point)
                 line
                 col
                 goal-num
                 goal-txt
                 (if (buffer-modified-p) 1 0)))
          (setq agda2-telemetry--last-event-id
                (caar (sqlite-select agda2-telemetry--db
                                     "SELECT last_insert_rowid()")))))
    (error nil)))

;;; --- Round Tracking (pre-command → Agda responses → post-command) ---

(defun agda2-telemetry--before-go (&rest _args)
  "Capture buffer state before an interaction round starts.
Installed as :before advice on `agda2-go'."
  (condition-case nil
      (when (and agda2-telemetry--db
                 agda2-telemetry--last-event-id
                 (null agda2-telemetry--current-event-id))
        (setq agda2-telemetry--current-event-id agda2-telemetry--last-event-id
              agda2-telemetry--pre-buffer (buffer-string)
              agda2-telemetry--pending-responses nil))
    (error nil)))

(defun agda2-telemetry--before-exec-response (response)
  "Collect a non-highlighting Agda response for the current round.
Installed as :before advice on `agda2-exec-response'."
  (condition-case nil
      (when (and agda2-telemetry--current-event-id
                 (symbolp (car-safe response))
                 (let ((name (symbol-name (car response))))
                   (not (string-prefix-p "agda2-highlight-" name))))
        (let ((func-name (symbol-name (car response)))
              (args-str (condition-case nil
                            (prin1-to-string (cdr response))
                          (error "<unserializable>"))))
          (push (cons func-name args-str)
                agda2-telemetry--pending-responses)))
    (error nil)))

(defun agda2-telemetry--after-run-last-commands ()
  "Finalize the interaction round when Agda processing completes.
Stores a snapshot if the buffer changed, and logs all collected responses.
Installed as :after advice on `agda2-run-last-commands'."
  (condition-case nil
      (when (and (not (buffer-local-value 'agda2-in-progress (current-buffer)))
                 agda2-telemetry--current-event-id
                 agda2-telemetry--db)
        (let ((event-id agda2-telemetry--current-event-id)
              (post-buffer (buffer-string)))
          ;; Store snapshot if buffer changed
          (when (and agda2-telemetry--pre-buffer
                     (not (string= agda2-telemetry--pre-buffer post-buffer)))
            (let ((diff (agda2-telemetry--compute-diff
                         agda2-telemetry--pre-buffer post-buffer)))
              (sqlite-execute
               agda2-telemetry--db
               "INSERT INTO snapshots (event_id, content) VALUES (?, ?)"
               (list event-id post-buffer))
              (when diff
                (sqlite-execute
                 agda2-telemetry--db
                 "UPDATE events SET extra = ? WHERE id = ?"
                 (list diff event-id)))))
          ;; Store collected responses
          (let ((seq 0))
            (dolist (resp (nreverse agda2-telemetry--pending-responses))
              (sqlite-execute
               agda2-telemetry--db
               "INSERT INTO responses (event_id, sequence, function_name, args_text)
                VALUES (?, ?, ?, ?)"
               (list event-id seq (car resp) (cdr resp)))
              (cl-incf seq)))
          ;; Clear round state
          (setq agda2-telemetry--current-event-id nil
                agda2-telemetry--pre-buffer nil
                agda2-telemetry--pending-responses nil)))
    (error nil)))

;;; --- Advice Installation ---

(defun agda2-telemetry--make-advice (cmd)
  "Return a :before advice function that logs CMD."
  (lambda (&rest _args)
    (agda2-telemetry--log cmd)))

(defun agda2-telemetry--install-advice ()
  "Advise all commands and internal dispatch points for telemetry."
  (unless agda2-telemetry--advised
    (when (boundp 'agda2-command-table)
      ;; Advise user commands
      (let ((seen (make-hash-table :test 'eq)))
        (dolist (entry agda2-command-table)
          (let ((cmd (car entry)))
            (when (and (symbolp cmd)
                       (not (gethash cmd seen))
                       (commandp cmd))
              (puthash cmd t seen)
              (advice-add cmd :before
                          (agda2-telemetry--make-advice cmd)
                          '((name . agda2-telemetry)))))))
      ;; Advise internal dispatch for round tracking
      (advice-add 'agda2-go :before
                  #'agda2-telemetry--before-go
                  '((name . agda2-telemetry)))
      (advice-add 'agda2-exec-response :before
                  #'agda2-telemetry--before-exec-response
                  '((name . agda2-telemetry)))
      (advice-add 'agda2-run-last-commands :after
                  #'agda2-telemetry--after-run-last-commands
                  '((name . agda2-telemetry)))
      (setq agda2-telemetry--advised t))))

(defun agda2-telemetry--remove-advice ()
  "Remove all telemetry advice."
  (when (and agda2-telemetry--advised (boundp 'agda2-command-table))
    (let ((seen (make-hash-table :test 'eq)))
      (dolist (entry agda2-command-table)
        (let ((cmd (car entry)))
          (when (and (symbolp cmd) (not (gethash cmd seen)))
            (puthash cmd t seen)
            (advice-remove cmd 'agda2-telemetry)))))
    (advice-remove 'agda2-go 'agda2-telemetry)
    (advice-remove 'agda2-exec-response 'agda2-telemetry)
    (advice-remove 'agda2-run-last-commands 'agda2-telemetry)
    (setq agda2-telemetry--advised nil)))

;;; --- Setup ---

(defun agda2-telemetry-setup ()
  "Initialize telemetry for the current Agda buffer."
  (when (and (derived-mode-p 'agda2-mode)
             buffer-file-name
             (fboundp 'sqlite-open))
    (setq agda2-telemetry--session-id
          (format "%s_%s" (format-time-string "%Y%m%d%H%M%S")
                  (substring (md5 (format "%s%s%s"
                                          (emacs-pid)
                                          (buffer-file-name)
                                          (float-time)))
                             0 8)))
    (agda2-telemetry--install-advice)
    (agda2-telemetry--ensure-db)
    ;; Store initial buffer state as a session-start snapshot
    (when agda2-telemetry--db
      (agda2-telemetry--log 'session-start)
      (when agda2-telemetry--last-event-id
        (sqlite-execute
         agda2-telemetry--db
         "INSERT INTO snapshots (event_id, content) VALUES (?, ?)"
         (list agda2-telemetry--last-event-id (buffer-string)))))
    (add-hook 'kill-buffer-hook #'agda2-telemetry--close-db nil t)))

(provide 'agda2-telemetry)
;;; agda2-telemetry.el ends here
