/* ─────────────────────────────────────────────────────────────────────
   upload.js — High-performance Chunked Video Upload (8 MB Slices)
   Bypasses reverse proxy and Cloudflare 100MB body limits seamlessly.
   Supports automatic retry on network glitch, live speed, and ETA.
   ───────────────────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('upload-form');
  if (!form) return;

  const fileInput    = document.getElementById('video-file');
  const dropZone     = document.getElementById('drop-zone');
  const progressWrap = document.getElementById('progress-wrap');
  const progressBar  = document.getElementById('progress-bar');
  const progressText = document.getElementById('progress-text');
  const submitBtn    = document.getElementById('submit-btn');
  const previewWrap  = document.getElementById('preview-wrap');
  const previewVid   = document.getElementById('preview-video');

  const CHUNK_SIZE = 8 * 1024 * 1024; // 8 MB per slice (well below any 100MB proxy limit)
  const MAX_RETRIES = 3;

  // ── Drag & Drop Handlers ──────────────────────────────────────────────
  ['dragenter', 'dragover'].forEach(ev => {
    dropZone?.addEventListener(ev, e => {
      e.preventDefault();
      dropZone.classList.add('drag-over');
    });
  });
  ['dragleave', 'drop'].forEach(ev => {
    dropZone?.addEventListener(ev, e => {
      e.preventDefault();
      dropZone.classList.remove('drag-over');
    });
  });
  dropZone?.addEventListener('drop', e => {
    const files = e.dataTransfer?.files;
    if (files?.[0]) {
      fileInput.files = files;
      handleFileSelected(files[0]);
    }
  });
  fileInput?.addEventListener('change', () => {
    if (fileInput.files[0]) handleFileSelected(fileInput.files[0]);
  });

  function handleFileSelected(file) {
    if (previewVid && previewWrap) {
      const url = URL.createObjectURL(file);
      previewVid.src = url;
      previewWrap.style.display = 'block';
    }
    const info = document.getElementById('file-info');
    if (info) {
      const mb = (file.size / 1024 / 1024).toFixed(1);
      const chunks = Math.ceil(file.size / CHUNK_SIZE);
      info.textContent = `📁 ${file.name} — ${mb} MB (${chunks} chunk${chunks !== 1 ? 's' : ''})`;
    }
  }

  function getCsrfToken() {
    const match = document.cookie.match(new RegExp('(^| )csrf_token=([^;]+)'));
    return match ? decodeURIComponent(match[2]) : (window.YTBot ? YTBot.getCsrfToken() : '');
  }

  // ── Upload Single Chunk with Progress & Retries ───────────────────────
  function uploadChunk(uploadId, chunkIndex, totalChunks, offset, chunkBlob, filename, onProgress) {
    return new Promise((resolve, reject) => {
      let attempts = 0;

      function attemptUpload() {
        attempts++;
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/videos/upload/chunk');
        xhr.setRequestHeader('X-CSRF-Token', getCsrfToken());

        const chunkData = new FormData();
        chunkData.append('upload_id', uploadId);
        chunkData.append('chunk_index', chunkIndex);
        chunkData.append('total_chunks', totalChunks);
        chunkData.append('offset', offset);
        chunkData.append('chunk', chunkBlob, filename);

        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable && onProgress) {
            onProgress(e.loaded);
          }
        };

        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve();
          } else {
            if (attempts < MAX_RETRIES) {
              console.warn(`Chunk ${chunkIndex} failed (HTTP ${xhr.status}), retrying attempt ${attempts + 1}...`);
              setTimeout(attemptUpload, 1500);
            } else {
              reject(new Error(`Chunk ${chunkIndex + 1}/${totalChunks} failed with HTTP ${xhr.status}: ${xhr.responseText}`));
            }
          }
        };

        xhr.onerror = () => {
          if (attempts < MAX_RETRIES) {
            console.warn(`Network error on chunk ${chunkIndex}, retrying attempt ${attempts + 1}...`);
            setTimeout(attemptUpload, 1500);
          } else {
            reject(new Error(`Network error uploading chunk ${chunkIndex + 1}/${totalChunks}`));
          }
        };

        xhr.send(chunkData);
      }

      attemptUpload();
    });
  }

  // ── Form Submit: Chunked Upload Pipeline ──────────────────────────────
  form.addEventListener('submit', async (e) => {
    e.preventDefault();

    const file = fileInput?.files[0];
    if (!file) {
      if (window.YTBot) YTBot.showToast('Please select a video file', 'error');
      else alert('Please select a video file');
      return;
    }

    // Convert comma-separated tags to JSON string
    const tagsInput = document.getElementById('tags-input');
    const tagsHidden = document.getElementById('tags-hidden');
    if (tagsInput && tagsHidden) {
      const arr = tagsInput.value.split(',').map(t => t.trim()).filter(Boolean);
      tagsHidden.value = JSON.stringify(arr);
    }

    submitBtn.disabled = true;
    submitBtn.textContent = 'Uploading… 0%';
    progressWrap.style.display = 'block';
    progressBar.style.width = '0%';
    progressBar.style.background = 'linear-gradient(90deg, #6366f1, #06b6d4)';

    const uploadId = 'up_' + Date.now() + '_' + Math.random().toString(36).substring(2, 10);
    const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
    let bytesUploadedTotal = 0;
    const startTime = Date.now();

    try {
      // 1. Upload chunks sequentially
      for (let i = 0; i < totalChunks; i++) {
        const offset = i * CHUNK_SIZE;
        const end = Math.min(file.size, offset + CHUNK_SIZE);
        const chunkBlob = file.slice(offset, end);
        let lastLoadedForChunk = 0;

        await uploadChunk(
          uploadId,
          i,
          totalChunks,
          offset,
          chunkBlob,
          file.name,
          (chunkLoaded) => {
            const delta = chunkLoaded - lastLoadedForChunk;
            lastLoadedForChunk = chunkLoaded;
            bytesUploadedTotal += delta;

            const pct = Math.min(99, Math.round((bytesUploadedTotal / file.size) * 100));
            progressBar.style.width = pct + '%';

            // Speed calculation
            const elapsedSec = (Date.now() - startTime) / 1000;
            const speedBytesPerSec = elapsedSec > 0 ? (bytesUploadedTotal / elapsedSec) : 0;
            const speedMb = (speedBytesPerSec / 1024 / 1024).toFixed(1);

            const uploadedMb = (bytesUploadedTotal / 1024 / 1024).toFixed(1);
            const totalMb = (file.size / 1024 / 1024).toFixed(1);

            progressText.textContent = `${pct}% — ${uploadedMb} MB / ${totalMb} MB (Chunk ${i + 1}/${totalChunks}) • ${speedMb} MB/s`;
            submitBtn.textContent = `Uploading… ${pct}%`;
          }
        );
      }

      // 2. All chunks sent successfully — Call Complete endpoint
      progressBar.style.width = '100%';
      progressText.textContent = '⚡ Assembling chunks and verifying video…';
      submitBtn.textContent = 'Processing…';

      const formData = new FormData(form);
      formData.delete('file'); // Don't send file again
      formData.append('upload_id', uploadId);
      formData.append('filename', file.name);
      formData.append('total_size', file.size);

      const completeResp = await fetch('/api/videos/upload/complete', {
        method: 'POST',
        headers: {
          'X-CSRF-Token': getCsrfToken(),
        },
        body: formData,
      });

      const data = await completeResp.json();

      if (completeResp.ok && data.ok) {
        progressBar.style.background = '#22c55e';
        progressText.textContent = '✅ Upload & processing complete!';
        if (window.YTBot) {
          YTBot.showToast('Video uploaded successfully! Opening Video Manager…', 'success');
        }
        setTimeout(() => {
          if (data.manage_url) {
            window.location.href = data.manage_url;
          } else {
            location.reload();
          }
        }, 800);
      } else {
        throw new Error(data.detail || 'Finalization failed');
      }

    } catch (err) {
      console.error('Upload failed:', err);
      submitBtn.disabled = false;
      submitBtn.textContent = '⬆️ Upload Video';
      progressBar.style.background = '#ef4444';
      progressText.textContent = '❌ ' + (err.message || 'Upload failed');
      if (window.YTBot) {
        YTBot.showToast(err.message || 'Upload failed', 'error');
      } else {
        alert(err.message || 'Upload failed');
      }
    }
  });
});
