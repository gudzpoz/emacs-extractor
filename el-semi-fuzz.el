;;; el-semi-fuzz.el --- Emacs Lisp (semi-)Fuzzing kit  -*- lexical-binding: t -*-

;; Copyright (C) 2025 gudzpoz

;; This program is free software; you can redistribute it and/or modify
;; it under the terms of the GNU General Public License as published by
;; the Free Software Foundation, either version 3 of the License, or
;; (at your option) any later version.

;; This program is distributed in the hope that it will be useful,
;; but WITHOUT ANY WARRANTY; without even the implied warranty of
;; MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
;; GNU General Public License for more details.

;; You should have received a copy of the GNU General Public License
;; along with this program.  If not, see <http://www.gnu.org/licenses/>.

;;; Commentary:

;; This program generates test cases for Emacs re-implementations to check
;; their compatibility/behavior against GNU Emacs.
;;
;; The program is expected to be run under a separate Emacs session (which is
;; why it uses `use-package' with `:vc' specified):
;;
;;    emacs -Q -nw -L . -l el-semi-fuzz --batch \
;;        --eval '(esfuzz-run-fuzz nil nil t)'
;;
;; Note that the tests generated mostly test *pure* functions and cannot
;; validate their side-effects. You might want to at least run some ERT tests
;; for that.
;;
;; ** Output format
;;
;; Each test entry is a printed ELisp list:
;;
;;     (KIND EXPR RESULT)
;;
;; KIND is an integer, either ?| (124, regular test) or ?! (33, error test). For
;; a regular test, its RESULT is the return value of EXPR when evaluated; for
;; error tests, EXPR will raise an error of RESULT type.
;;
;; When outputing to a file, all entries are simply concatenated together, which
;; one may use `read' to recover. When outputing to stdout (with the STREAM
;; argument of `esfuzz-run-fuzz' set to t), to facilitate streamlined
;; processing, the format is modified into:
;;
;;     LENGTH (KIND EXPR RESULT)
;;
;; LENGTH is the length (in bytes) of the printed entry list.

;;; Code:

(require 'cl-extra)
(require 'cl-macs)
(require 'cl-print)
(require 'json)
(require 'pcase)
(require 'seq)
(cl-float-limits)

(cl-defmethod cl-print-object ((object symbol) stream)
  (if (or (null object) (intern-soft object))
      (prin1 object stream)
    (princ "#:" stream)
    (unless (length= (symbol-name object) 0)
      (prin1 object stream))))

(package-initialize)
(setq debug-on-error t)
(use-package promise
  :ensure
  :vc (:url "https://github.com/chuntaro/emacs-promise.git"))

(defgroup esfuzz ()
  "ESFuzz, a Emacs Lisp semi-fuzzing kit, for Emacs re-implementers."
  :prefix "esfuzz-"
  :group 'lisp)

(defcustom esfuzz-extraction-json-path "extraction.json"
  "The extraction.json file produced by emacs-extractor."
  :type '(string))
(defcustom esfuzz-output-file-path "esfuzz-generated.el"
  "The test instance file produced by esfuzz."
  :type '(string))

(defvar esfuzz--extraction nil
  "Contents of extraction.json. Set by `esfuzz-run-fuzz' during testing.")
(defun esfuzz--subroutines-per-file ()
  "Reads extraction.json and extract subroutine data."
  (let ((extraction (with-temp-buffer
                      (insert-file-contents esfuzz-extraction-json-path)
                      (goto-char (point-min))
                      (json-parse-buffer :object-type 'alist :array-type 'list))))
    (setq esfuzz--extraction extraction)
    (mapcar
     (lambda (item) (cons (cdr (assq 'file item))
                          (cdr (assq 'functions item))))
     (cdr (assq 'file_extractions extraction)))))

(defsubst esfuzz--arg-num-to-symbol (num)
  (cond
   ((natnump num) num)
   ((= -2 num) 'many)
   ((= -1 num) 'unevalled)
   (t (error "unexpected arg num (%d)" num))))

(defmacro esfuzz--define-extraction-accessors (&rest field-list)
  "Creates accessor functions for data in `esfuzz--subroutines-per-file'."
  (let (code)
    (dolist (spec field-list (cons 'progn code))
      (pcase-let* ((`(,field-name ,json-field ,post-process) spec)
                   (accessor-name (intern (concat "esfuzz--subr-" (symbol-name field-name))))
                   (docstring (format "Access %s field in a subroutine from extraction.json." json-field))
                   (form `(cdr (assq ',json-field arg)))
                   (form (if post-process `(funcall #',post-process ,form) form)))
        (push `(defsubst ,accessor-name (arg) ,docstring ,form) code)))))

(esfuzz--define-extraction-accessors
 (lisp-name lisp_name intern)
 (min-args min_args esfuzz--arg-num-to-symbol)
 (max-args max_args esfuzz--arg-num-to-symbol)
 (int-spec int_spec nil)
 (args args nil))

(defun esfuzz--get-unadviced-subr (function)
  "Get an unadviced function.

`func-arity' can be inaccurate for functions with advices, and this
function serves to get the inner most unadviced function. This is only
tested on Emacs 30 and might not run on other versions."
  (if (subrp function)
      function
    (while (advice--p function)
      (setq function (advice--cdr function)))
    function))

(defun esfuzz--validate-arities (file-subroutines)
  (let (void-functions)
    (pcase-dolist (`(,file . ,subroutines) file-subroutines)
      (dolist (subr subroutines)
        (let* ((name (esfuzz--subr-lisp-name subr))
               (min-args (esfuzz--subr-min-args subr))
               (max-args (esfuzz--subr-max-args subr))
               (extracted-arity (cons min-args max-args))
               (function (symbol-function name))
               (arity (and function (func-arity (esfuzz--get-unadviced-subr function)))))
          (if (null function)
              (push name void-functions)
            (unless (equal arity extracted-arity)
              (warn "invalid arity for %s (%s), extracted %S, actual %S"
                    name file extracted-arity arity))))))
    (when void-functions
      (warn "void-functions: %S" void-functions))))

(defun esfuzz--integer-generator (from to)
  "Returns a function that randomly generates integers."
  (lambda () (+ from (random (1+ (- to from))))))
(defun esfuzz--float-generator (to)
  "Returns a functions that randomly generates floats."
  (let ((int-gen (esfuzz--integer-generator 0 to)))
    (lambda () (read (format "%s%d.%d"
                             (if (= 0 (random 2)) "" "-")
                             (funcall int-gen) (funcall int-gen))))))
(defun esfuzz--string-generator (from-length to-length unibyte)
  "Generates random strings."
  (let ((length-gen (esfuzz--integer-generator from-length to-length))
        (char-gen (pcase unibyte
                    ('ascii (esfuzz--integer-generator 0 255))
                    ('t (esfuzz--integer-generator 0 255))
                    (`(,from . ,to) (esfuzz--integer-generator from to))
                    (_ (esfuzz--integer-generator 0 (max-char))))))
    (lambda ()
      (let ((length (funcall length-gen)))
        (with-temp-buffer
          (set-buffer-multibyte (not unibyte))
          (dotimes (_ length)
            (insert (funcall char-gen)))
          (buffer-string))))))
(defvar esfuzz--all-symbols nil)
(defun esfuzz--symbol-generator (status)
  "Returns random symbols.

STATUS can be :unintern, :intern or :variable."
  (pcase status
    (:unintern
     (let ((string-gen (esfuzz--string-generator 0 8 '(?\  . ?~))))
       (lambda () (make-symbol (funcall string-gen)))))
    (:intern
     (lambda ()
       (unless esfuzz--all-symbols
         (let ((symbols (delq nil (mapcar (lambda (data) (intern-soft (cdr (assq 'lisp_name data))))
                                          (cdr (assq 'all_symbols esfuzz--extraction))))))
           (setq esfuzz--all-symbols (vconcat symbols))))
       (aref esfuzz--all-symbols (random (length esfuzz--all-symbols)))))
    (:variable
     (let (all-symbols)
       (lambda ()
         (unless all-symbols
           (let ((symbols (seq-mapcat (lambda (data) (cdr (assq 'lisp_variables data)))
                                      (cdr (assq 'file_extractions esfuzz--extraction)))))
             (setq all-symbols (vconcat (delq nil (mapcar #'esfuzz--subr-lisp-name symbols))))))
         (aref all-symbols (random (length all-symbols))))))))
(defun esfuzz--cons-generator ()
  "Returns a cons."
  (lambda () (cons (esfuzz--random-object) (esfuzz--random-object))))
(defun esfuzz--seq-generator (constructor &optional max-length)
  "Returns a cons.

The CONSTRUCTOR is to be called like (funcall CONSTRUCTOR random-list)."
  (let ((int-gen (esfuzz--integer-generator 0 (or max-length 100))))
    (lambda ()
      (let* ((length (funcall int-gen))
             (list (mapcar #'esfuzz--random-object (make-list length nil))))
        (funcall constructor list)))))

(defconst esfuzz--circular-list '#1=(1 . #1#)
  "A circular list used for testing.")
(defconst esfuzz--basic-arg-types
  `((null . (:nil (nil)))

    (small-natnump . (:nil
                      (0 1 2 3 4 5 6 7 8 9)
                      (?a ?A ?z ?Z ?\\ ?\ )
                      ,(esfuzz--integer-generator 0 127)))
    (small-fixnump . (:nil
                      (-1 -2 -3 -4 -5 -6 -7 -8 -9)
                      ,(esfuzz--integer-generator -128 0)))

    (characterp . (:nil
                   small-natnump
                   (,(unibyte-char-to-multibyte 127)
                    ,(unibyte-char-to-multibyte 128)
                    ,(unibyte-char-to-multibyte 129))
                   (,(max-char) ,(1- (max-char)))
                   ,(esfuzz--integer-generator 0 (max-char))))
    (natnump . (:nil
                characterp
                (,most-positive-fixnum
                 ,(1+ most-positive-fixnum)
                 ,(1- most-positive-fixnum))
                ,(esfuzz--integer-generator 0 (* 2 most-positive-fixnum))))
    (wholenump . (:nil natnump))
    (fixnump . (:nil
                characterp
                small-fixnump
                (,most-negative-fixnum
                 ,(1+ most-negative-fixnum))
                (,most-positive-fixnum
                 ,(1- most-positive-fixnum))
                ,(esfuzz--integer-generator most-negative-fixnum most-positive-fixnum)))
    (bignump . (:nil
                (,(1+ most-positive-fixnum)
                 ,(1- most-negative-fixnum)
                 ,(* 2 most-positive-fixnum)
                 ,(* 2 most-negative-fixnum))
                ,(esfuzz--integer-generator (* 2 most-negative-fixnum)
                                            (1- most-positive-fixnum))
                ,(esfuzz--integer-generator (1+ most-positive-fixnum)
                                            (* 2 most-positive-fixnum))))
    (integerp . (:nil fixnump bignump))

    (floatp . (:nil
               (0.0 1.0 -1.0)
               ,(list float-e float-pi)
               ,(list cl-float-epsilon cl-float-negative-epsilon)
               ,(list cl-most-positive-float cl-most-positive-float)
               ,(list cl-least-positive-float cl-least-negative-float)
               ,(list cl-least-positive-normalized-float cl-least-negative-normalized-float)
               (1.0e+NaN -1.0e+NaN 0.0e+NaN -0.0e+NaN 10.0e+NaN -10.0e+NaN)
               (1.0e+INF -1.0e+INF 0.0e+INF -0.0e+INF 10.0e+INF -10.0e+INF)
               ,(esfuzz--float-generator 10)))
    (numberp . (:nil integerp floatp))

    (stringp . (:nil
                ("" "Hello World!")
                ("\0" "\\" "\n" "\"")
                ("中文" "日本語" "민족어" "🤗")
                ("\377" "🤗\377")
                ,(esfuzz--string-generator 0 10 'ascii)
                ,(esfuzz--string-generator 0 10 t)
                ,(esfuzz--string-generator 0 10 nil)
                ,(esfuzz--string-generator 20 40 nil)
                ,(esfuzz--string-generator 20 40 t)))
    (symbolp . (:nil
                (nil t ##)
                (characterp natnump integerp stringp symbolp)
                (a b c d e f g)
                (\1 \2 \3)
                (\` \' \#\' ,(quote \,) ,(quote \,@) \ )
                (#:nil #:t)
                (#_nil #_t)
                ,(esfuzz--symbol-generator :intern)
                ,(esfuzz--symbol-generator :unintern)
                ,(esfuzz--symbol-generator :variable)))

    (consp . (:sequencep
              ((nil . nil) (1 . 1))
              (,esfuzz--circular-list)
              ,(esfuzz--cons-generator)
              ,(esfuzz--seq-generator #'identity)))
    (listp . (:sequencep consp (nil)))
    (vectorp . (:sequencep
                ([] [1] [nil] [t] [1 2 3])
                ,(esfuzz--seq-generator #'vconcat)))
    (arrayp . (:sequencep vectorp stringp))
    (sequencep . (:sequencep listp arrayp)))
  "Typical arguments to pass to functions.

An association list, with a key being type predicates and a value being
a cons of a flag and a list of value providers. Possible values
providers:

- A symbol, which must be a type predicate, meaning its a sub type,
  whose generated values are also validate value for the current type.
- A list of values, which will be provided randomly.
- A function, which will be called without args to produce a value.")
(defun esfuzz--inline-basic-arg-types ()
  "Preprocess `esfuzz--basic-arg-types', making, e.g., `natnump' includes
entries from `characterp'."
  ;; Inlining
  (dolist (cons esfuzz--basic-arg-types)
    (setcdr
     (cdr cons)
     (seq-mapcat
      (lambda (item)
        (if (symbolp item) (cddr (assq item esfuzz--basic-arg-types)) (list item)))
      (cddr cons))))
  ;; Checking
  (pcase-dolist (`(,pred . ,items) esfuzz--basic-arg-types)
    (when-let* ((remaining (seq-filter #'symbolp (cdr items))))
      (error "wrong order in `esfuzz--basic-arg-types' (pred: `%S', subtypes: `%S')"
             pred remaining)))
  ;; Copy generator functions so that they are more likely to be chosen
  (dolist (cons esfuzz--basic-arg-types)
    (let* ((list (cddr cons))
           (length (length list))
           (generators (seq-filter #'atom list))
           (generator-count (length generators)))
      (unless (zerop generator-count)
        (dotimes (_ (floor (/ (- length generator-count) generator-count)))
          (setq list (append generators list)))
        (setcdr (cdr cons) list)))))

(defvar esfuzz--random-depth nil)
(defcustom esfuzz-random-seq-level 3
  "Max nested sequence depth."
  :type '(natnum))
(defsubst esfuzz--random-in-list (list)
  "Get a random item from a list."
  (let ((length (length list)))
    (nth (random length) list)))
(defsubst esfuzz--random-arg-type (predicate)
  (cond
   (predicate (cddr (assq predicate esfuzz--basic-arg-types)))
   ((<= esfuzz--random-depth esfuzz-random-seq-level)
    (cddr (esfuzz--random-in-list esfuzz--basic-arg-types)))
   (t (let (items)
        (while (not items)
          (setq items (cdr (esfuzz--random-in-list esfuzz--basic-arg-types)))
          (if (eq (car items) :sequencep) (setq items nil)))
        (cdr items)))))
(defsubst esfuzz--random-arg (items no-generator)
  (let (item item-ok)
    (while (not item-ok)
      (setq item (esfuzz--random-in-list items)
            item-ok (not (and no-generator (atom item)))))
    item))
(defun esfuzz--random-object (&optional predicate no-generator)
  "Generate a random object."
  (let* ((esfuzz--random-depth (1+ (or esfuzz--random-depth 0)))
         (items (esfuzz--random-arg-type predicate))
         (entry (esfuzz--random-arg items no-generator)))
    (pcase entry
      ((pred listp)
       (let ((o (esfuzz--random-in-list entry)))
         (cond
          ((eq o esfuzz--circular-list)
           (setq o (cons 1 nil))
           (setcdr o o))
          ((consp o)
           (let ((copy (cons (car o) (cdr o))))
             (setq o copy)
             (while (consp (cdr copy))
               (setq copy (setcdr copy (cons (cadr copy) (cddr copy)))))))
          ((and o (symbolp o) (not (intern-soft o)))
           (setq o (make-symbol (symbol-name o)))))
         o))
      ((pred atom) (funcall entry))
      (_ (error "invalid `esfuzz--basic-arg-types': %S" entry)))))

(defcustom esfuzz-files-to-fuzz
  '("alloc.c" "data.c" "fns.c" "floatfns.c")
  "Filenames, the subroutines of which will be tested."
  :type '(repeat string))
(defcustom esfuzz-function-blocklist
  '(;; crash
    get-variable-watchers
    ;; behavior change
    set set-default
    fset defalias)
  "Functions to not run tests for, because, for example, it crashes Emacs."
  :type '(repeat symbol))
(defcustom esfuzz-skip-error-types
  '(wrong-type-argument overflow-error)
  "Whether to reduce test runs when some special errors are thrown."
  :type '(repeat symbol))
(defun esfuzz--skip-too-large (arg-slot upper)
  (lambda (args types)
    (if (not args)
        (not (eq 'bignump (nth arg-slot types)))
      (or (not (integerp (nth arg-slot args)))
          (<= (nth arg-slot args) upper)))))
(defcustom esfuzz-skip-arg-types
  `((make-list   . ,(esfuzz--skip-too-large 0 255))
    (make-string . ,(esfuzz--skip-too-large 0 255))
    (make-vector . ,(esfuzz--skip-too-large 0 255))
    (make-record . ,(esfuzz--skip-too-large 1 255))
    (make-bool-vector . ,(esfuzz--skip-too-large 0 1024)))
  "Predicates to filter out test cases."
  :type '(alist :key-type symbol :value-type (cons symbol function)))
(defcustom esfuzz-function-max-args 3
  "Max args of functions."
  :type '(natnum))
(defcustom esfuzz-runs-per-combination 50
  "Test runs per type combination."
  :type '(natnum))

(defsubst esfuzz--combination-generator (arg-count)
  (let* ((items (length esfuzz--basic-arg-types))
         (current (make-list arg-count 0))
         ended)
    (lambda ()
      (if (or ended (= 0 arg-count))
          :esfuzz--ended
        (prog1
            (mapcar (lambda (i) (car (nth i esfuzz--basic-arg-types))) current)
          (let ((indices current))
            (setcar indices (1+ (car indices)))
            (while (and (not ended)
                        (>= (car indices) items))
              (setcar indices 0)
              (setq indices (cdr indices))
              (if indices
                  (setcar indices (1+ (car indices)))
                (setq ended t)))))))))
(defun esfuzz--run-fuzz-function (function)
  "Fuzz-test one function."
  (let* ((name (esfuzz--subr-lisp-name function))
         (min-args (esfuzz--subr-min-args function))
         (max-args (esfuzz--subr-max-args function))
         (f (esfuzz--get-unadviced-subr (symbol-function name)))
         (filter (cdr (assq name esfuzz-skip-arg-types)))
         arg-count arg-types args combinations combination)
    (unless (or (not f)
                (and (= min-args 0) (equal max-args 0))
                (> min-args esfuzz-function-max-args)
                (eq max-args 'unevalled)
                (memq name esfuzz-function-blocklist))
      (setq arg-count (cond
                       ((> min-args 0) min-args)
                       ((eq max-args 'many) esfuzz-function-max-args)
                       (t (min max-args esfuzz-function-max-args)))
            arg-types (make-list arg-count nil)
            combinations (esfuzz--combination-generator arg-count))
      (setq combination (funcall combinations))
      (while (not (eq combination :esfuzz--ended))
        (let ((i 0)
              (runs (* esfuzz-runs-per-combination
                       (seq-count (lambda (type) (not (eq type 'null))) combination)))
              marker result)
          (if (and filter (not (funcall filter nil combination)))
              (setq i runs))
          (while (< i runs)
            (setq args (mapcar (lambda (type) (esfuzz--random-object type (< i (/ runs 2)))) combination))
            (when (or (not filter) (funcall filter args combination))
              (condition-case err
                  (setq result (apply f args)
                        marker "|")
                (error (setq marker "!"
                             result (car err))
                       (when (memq (car err) esfuzz-skip-error-types)
                         (setq runs (/ runs 8)))))
              (princ marker)
              (princ " ")
              (cl-prin1 (cons name (mapcar (lambda (arg) `',arg) args)))
              (princ " ")
              (cl-prin1 result)
              (princ "\n"))
            (setq i (1+ i))))
        (setq combination (funcall combinations))))
    (message ": %S" name)))
(defconst esfuzz--worker-end-message ": nil")
(defun esfuzz--worker-repl ()
  "Read values from input and execute tests. Many functions in Emacs, if
called wrong, can crash the whole process. So we instead run the tests
in worker Emacs sessions.

The communication protocol is simple. The controller encodes the
function by double-prin1 it (because `read-minibuffer' cannot read
across new lines), and the worker will print back tests and results (see
`esfuzz--run-fuzz-function' for the format)."
  (let ((print-circle t))
    (condition-case nil
        (while-let ((function (read (read-minibuffer ""))))
          (esfuzz--run-fuzz-function function))
      (end-of-file (message esfuzz--worker-end-message)))))

(defcustom esfuzz-worker-count 8
  "Worker count."
  :type '(natnum))
(defcustom esfuzz-worker-reruns 4
  "Parallel running several workers for the same function."
  :type '(natnum))
(defvar esfuzz--stream-output nil
  "Whether to print to stdout.")
(defvar esfuzz--script-file load-file-name
  "The script file.")
(defvar esfuzz--running-workers 0)
(defvar esfuzz--worker-queue nil)
(defun esfuzz--semaphore-promise ()
  (if (> esfuzz-worker-count esfuzz--running-workers)
      (prog1 (promise-new (lambda (resolve _) (funcall resolve nil)))
        (setq esfuzz--running-workers (1+ esfuzz--running-workers)))
    (promise-new (lambda (resolve _) (push resolve esfuzz--worker-queue)))))
(defun esfuzz--semaphore-release ()
  (if-let ((resolve (pop esfuzz--worker-queue)))
      (funcall resolve nil)
    (setq esfuzz--running-workers (1- esfuzz--running-workers))))

(defun esfuzz--worker-sentinel (process _)
  (when (not (process-live-p process))
    (esfuzz--finalize-promises process)))
(defun esfuzz--create-worker ()
  (let* ((buffer (generate-new-buffer " *esfuzz-worker*" t))
         (id (buffer-name buffer))
         (process-connection-type nil)
         (worker-eval (format "(esfuzz--worker-run '%S)"
                              `((esfuzz-extraction-json-path . ,esfuzz-extraction-json-path)
                                (esfuzz-random-seq-level     . ,esfuzz-random-seq-level)
                                (esfuzz-function-blocklist   . ,esfuzz-function-blocklist)
                                (esfuzz-function-max-args    . ,esfuzz-function-max-args)
                                (esfuzz-runs-per-combination . ,esfuzz-runs-per-combination)))))
    (make-process :name id :buffer buffer
                  :command (list
                            "emacs" "-Q" "-nw" "--batch"
                            "-L" (file-name-parent-directory esfuzz--script-file)
                            "-l" "el-semi-fuzz"
                            "--eval" worker-eval)
                  :sentinel #'esfuzz--worker-sentinel
                  :stderr "*Messages*")))

(defun esfuzz--worker-promise-send (resolve reject function)
  (promise-then
   (esfuzz--semaphore-promise)
   (lambda (_)
     (let* ((name (esfuzz--subr-lisp-name function))
            (message (prin1-to-string (prin1-to-string function)))
            (message (string-replace "\r" "\\r" (string-replace "\n" "\\n" message)))
            (process (esfuzz--create-worker)))
       (process-put process 'esfuzz-worker-data (list resolve reject name function))
       (process-send-string process message)
       (process-send-string process "\n")
       (process-send-eof process)))))

(defun esfuzz--finalize-promises (process)
  "Called when a process ends to update all promises."
  (with-current-buffer (process-buffer process)
    (cl-assert (not (process-live-p process)))
    (cl-assert (= (point) (point-max)))
    (goto-char (point-min))
    (pcase-let ((`(,resolve ,reject ,name ,function)
                 (process-get process 'esfuzz-worker-data))
                (results nil))
      (condition-case err
          (while (< (point) (point-max))
            (let ((marker (char-after)) call)
              (forward-char)
              (if (= ?: marker)
                  (read (current-buffer))
                (condition-case nil
                    (progn
                      (setq call (read (current-buffer)))
                      (let* ((entry (list marker call (read (current-buffer)))))
                        (push entry results)))
                  (error
                   (message "read: %S\n%s" call
                            (buffer-substring (line-beginning-position -1)
                                              (line-end-position 1)))
                   (if (re-search-forward "\n[|!:]" nil t)
                       (backward-char 2)
                     (goto-char (point-max)))))))
            (forward-char))
        (:success (funcall resolve (cons name results)))
        (error (message "call: %S" (prin1-to-string function))
               (funcall reject (cons name results)))))))

(defun esfuzz--send-to-worker (function)
  "Send input to a worker."
  (promise-chain
      (promise-new
       (lambda (resolve reject)
         (esfuzz--worker-promise-send resolve reject function)))
    (then (lambda (info)
            (if (not esfuzz--stream-output)
                info
              (dolist (i (cdr info) nil)
                (let* ((print-circle t)
                       (s (cl-prin1-to-string i)))
                  (prin1 (string-bytes s))
                  (princ " ")
                  (princ s)
                  (princ "\n"))))))
    (finally (lambda () (esfuzz--semaphore-release)))))

(defun esfuzz--gather-worker-output (promises)
  "Gather outputs."
  (with-temp-file esfuzz-output-file-path
    (let* ((output (current-buffer))
           (n (length promises))
           results-ready
           (push (lambda (result)
                   (when (not esfuzz--stream-output)
                     (dolist (entry (cdr result))
                       (let ((print-circle t))
                         (cl-prin1 entry output))))
                   (when (zerop (setq n (1- n)))
                     (setq results-ready t)))))
      (dolist (promise promises)
        (promise-then promise push push))
      (when (length> promises 0)
        (while (not results-ready)
          (sleep-for 0.25))))))

(defun esfuzz--worker-run (config-alist)
  (dolist (pair config-alist)
    (set (car pair) (cdr pair)))
  (esfuzz--subroutines-per-file)
  (esfuzz--inline-basic-arg-types)
  (esfuzz--worker-repl))

(defun esfuzz-run-fuzz (&optional file-selector func-selector stream)
  "Run the fuzz test.

FILE-SELECTOR / FUNC-SELECTOR are regexps selecting files / functions to
generate test cases for.

If STREAM is t, instead of writing to a output file, print directly to
stdout. Useful when testing many functions, which might produce GBs of
test cases."
  (cond
   ((featurep 'native-compile)
    (unless (native-comp-function-p (symbol-function 'esfuzz-run-fuzz))
      (native-compile (string-replace ".elc" ".el" esfuzz--script-file))))
   ((not (byte-code-function-p (symbol-function 'esfuzz-run-fuzz)))
    (byte-compile-file esfuzz--script-file)))
  (let ((print-circle t)
        (esfuzz--stream-output stream)
        (file-functions (esfuzz--subroutines-per-file))
        promises)
    (esfuzz--inline-basic-arg-types)
    (esfuzz--validate-arities file-functions)
    (unwind-protect
        (progn
          (dolist (file esfuzz-files-to-fuzz)
            (when (or (not file-selector) (string-match-p file-selector file))
              (let ((functions (cdr (assoc file file-functions))))
                (dolist (f functions)
                  (when (or (not func-selector)
                            (string-match-p func-selector
                                            (cdr (assq 'lisp_name f))))
                    (dotimes (_ esfuzz-worker-reruns)
                      (push (esfuzz--send-to-worker f) promises))))))))
      (esfuzz--gather-worker-output (nreverse promises)))
    ;; In case of any errors, we leave `esfuzz--extraction' be for debugging.
    ;; So the following line is not in `unwind-protect' on purpose
    (setq esfuzz--extraction nil)))

(provide 'el-semi-fuzz)
