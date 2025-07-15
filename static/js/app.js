// Global Variables
let currentItemId = null;
let selectedPosters = {};
let loadingModal = null;
let posterModal = null;
let resultsModal = null;

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', function() {
    // Initialize Bootstrap modals
    loadingModal = new bootstrap.Modal(document.getElementById('loadingModal'));
    posterModal = new bootstrap.Modal(document.getElementById('posterModal'));
    resultsModal = new bootstrap.Modal(document.getElementById('resultsModal'));
    var dropdownToggles = document.querySelectorAll('[data-bs-toggle="dropdown"]');
    dropdownToggles.forEach(function(toggle) {
        new bootstrap.Dropdown(toggle, {
            popperConfig: function(defaultBsPopperConfig) {
                defaultBsPopperConfig.modifiers.push({
                    name: 'appendToBody',
                    enabled: true,
                    phase: 'write',
                    fn({ state }) {
                        if (state.elements && state.elements.popper && state.elements.popper.parentNode !== document.body) {
                            document.body.appendChild(state.elements.popper);
                        }
                    }
                });
                return defaultBsPopperConfig;
            }
        });
    });
    console.log('Jellyfin Poster Manager initialized');
});

// Update loadPosters function to show conversion progress
async function loadPosters(itemId) {
    currentItemId = itemId;
    
    // Show loading modal with conversion message
    document.getElementById('loadingText').textContent = 'Searching and converting posters...';
    loadingModal.show();
    
    try {
        const response = await fetch(`/item/${itemId}/posters`);
        const data = await response.json();
        
        if (data.error) {
            throw new Error(data.error);
        }
        
        // Hide loading modal
        loadingModal.hide();
        
        // Display posters
        displayPosters(data.item, data.posters);
        
    } catch (error) {
        console.error('Error loading posters:', error);
        loadingModal.hide();
        showAlert('Failed to load posters: ' + error.message, 'danger');
    }
}

// Display posters in modal with base64 images
function displayPosters(item, posters) {
    const modalBody = document.getElementById('posterModalBody');
    
    if (posters.length === 0) {
        modalBody.innerHTML = `
            <div class="text-center py-5">
                <i class="fas fa-search fa-3x text-muted mb-3"></i>
                <h5 class="text-muted">No posters found</h5>
                <p class="text-muted">No posters were found for "${item.title}"</p>
            </div>
        `;
    } else {
        let html = `
            <div class="mb-3">
                <h6><i class="fas fa-film me-2"></i>${item.title}</h6>
                <small class="text-muted">${item.year || 'Unknown Year'} • ${item.type}</small>
                <small class="text-muted ms-3">
                    <i class="fas fa-images me-1"></i>
                    Found ${posters.length} poster${posters.length !== 1 ? 's' : ''}
                </small>
            </div>
            <div class="row">
        `;
        
        posters.forEach((poster, index) => {
            // Use base64 image if available, otherwise show placeholder
            const imageSource = poster.base64 || 'data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMzAwIiBoZWlnaHQ9IjQ1MCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cmVjdCB3aWR0aD0iMTAwJSIgaGVpZ2h0PSIxMDAlIiBmaWxsPSIjZjBmMGYwIi8+PHRleHQgeD0iNTAlIiB5PSI1MCUiIGZvbnQtZmFtaWx5PSJBcmlhbCIgZm9udC1zaXplPSIxNiIgZmlsbD0iIzY2NiIgdGV4dC1hbmNob3I9Im1pZGRsZSIgZHk9Ii4zZW0iPkltYWdlIE5vdCBBdmFpbGFibGU8L3RleHQ+PC9zdmc+';
            
            html += `
                <div class="col-lg-2 col-md-3 col-sm-4 col-6 mb-3">
                    <div class="card poster-card h-100" data-poster-id="${poster.id}" onclick="selectPoster('${poster.url}', ${poster.id})">
                        <div class="poster-container">
                            ${!poster.base64 ? `
                                <div class="poster-loading d-flex align-items-center justify-content-center">
                                    <div class="text-center">
                                        <i class="fas fa-exclamation-triangle text-warning fa-2x mb-2"></i>
                                        <br>
                                        <small class="text-muted">Image failed to load</small>
                                    </div>
                                </div>
                            ` : ''}
                            
                            <img src="${imageSource}" 
                                 class="card-img-top poster-image" 
                                 alt="Poster ${index + 1}" 
                                 loading="lazy"
                                 style="${!poster.base64 ? 'display: none;' : ''}">
                        </div>
                        
                        <div class="card-body p-2">
                            <small class="text-muted d-block text-truncate" title="${poster.title}">
                                ${poster.title}
                            </small>
                            <div class="d-flex justify-content-between align-items-center mt-1">
                                <small class="text-muted">
                                    <i class="fas fa-user me-1"></i>${poster.uploader}
                                </small>
                                ${poster.likes ? `
                                    <small class="text-muted">
                                        <i class="fas fa-heart me-1 text-danger"></i>${poster.likes}
                                    </small>
                                ` : ''}
                            </div>
                        </div>
                    </div>
                </div>
            `;
        });
        
        html += '</div>';
        modalBody.innerHTML = html;
    }
    
    // Show modal
    posterModal.show();
}

// Select a poster
async function selectPoster(posterUrl, posterId) {
    try {
        // Visual feedback
        document.querySelectorAll('.poster-card').forEach(card => {
            card.classList.remove('selected');
        });
        document.querySelector(`[data-poster-id="${posterId}"]`).classList.add('selected');
        
        // Send selection to server
        const response = await fetch(`/item/${currentItemId}/select`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ poster_url: posterUrl })
        });
        
        const data = await response.json();
        
        if (data.success) {
            // Store selection locally
            selectedPosters[currentItemId] = posterUrl;
            
            // Update UI
            updateItemStatus(currentItemId, 'selected');
            
            // Close modal after short delay
            setTimeout(() => {
                posterModal.hide();
            }, 500);
            
            // Update upload all button
            updateUploadAllButton();
            
        } else {
            throw new Error(data.error || 'Failed to select poster');
        }
        
    } catch (error) {
        console.error('Error selecting poster:', error);
        showAlert('Failed to select poster: ' + error.message, 'danger');
    }
}

// Update item status in UI
function updateItemStatus(itemId, status) {
    const statusElement = document.getElementById(`status-${itemId}`);
    const itemCard = document.querySelector(`[data-item-id="${itemId}"]`);
    
    switch (status) {
        case 'selected':
            statusElement.innerHTML = `
                <span class="badge status-selected">
                    <i class="fas fa-check me-1"></i>Selected
                </span>
                <button class="btn btn-warning btn-sm mt-1 w-100" onclick="uploadPoster('${itemId}')">
                    <i class="fas fa-cloud-upload-alt me-1"></i>Upload Now
                </button>
            `;
            itemCard.classList.add('selected');
            break;
            
        case 'uploading':
            statusElement.innerHTML = `
                <span class="badge bg-info">
                    <i class="fas fa-spinner fa-spin me-1"></i>Uploading...
                </span>
            `;
            break;
            
        case 'uploaded':
            statusElement.innerHTML = `
                <span class="badge status-uploaded">
                    <i class="fas fa-check-circle me-1"></i>Uploaded!
                </span>
            `;
            itemCard.classList.remove('selected');
            delete selectedPosters[itemId];
            updateUploadAllButton();
            break;
            
        case 'error':
            statusElement.innerHTML = `
                <span class="badge status-error">
                    <i class="fas fa-exclamation-triangle me-1"></i>Error
                </span>
                <button class="btn btn-outline-warning btn-sm mt-1 w-100" onclick="uploadPoster('${itemId}')">
                    <i class="fas fa-redo me-1"></i>Retry Upload
                </button>
            `;
            break;
    }
}

// Upload individual poster
async function uploadPoster(itemId) {
    updateItemStatus(itemId, 'uploading');
    
    try {
        const response = await fetch(`/upload/${itemId}`, {
            method: 'POST'
        });
        
        const data = await response.json();
        
        if (data.success) {
            updateItemStatus(itemId, 'uploaded');
            showAlert('Poster uploaded successfully!', 'success');
        } else {
            updateItemStatus(itemId, 'error');
            showAlert('Upload failed: ' + data.error, 'danger');
        }
        
    } catch (error) {
        console.error('Error uploading poster:', error);
        updateItemStatus(itemId, 'error');
        showAlert('Upload failed: ' + error.message, 'danger');
    }
}

// Upload all selected posters
async function uploadAllSelected() {
    const selectedCount = Object.keys(selectedPosters).length;
    
    if (selectedCount === 0) {
        showAlert('No posters selected', 'warning');
        return;
    }
    
    // Confirm action
    if (!confirm(`Upload ${selectedCount} selected poster(s)?`)) {
        return;
    }
    
    // Show progress
    const progressContainer = document.getElementById('progressContainer');
    const progressBar = document.getElementById('progressBar');
    const progressText = document.getElementById('progressText');
    
    progressContainer.style.display = 'block';
    progressBar.style.width = '0%';
    progressText.textContent = '0%';
    
    // Disable upload button
    const uploadBtn = document.getElementById('uploadAllBtn');
    uploadBtn.disabled = true;
    uploadBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i>Uploading...';
    
    try {
        const response = await fetch('/upload-all', {
            method: 'POST'
        });
        
        const data = await response.json();
        
        if (data.results) {
            // Update progress
            progressBar.style.width = '100%';
            progressText.textContent = '100%';
            
            // Process results
            let successCount = 0;
            let failCount = 0;
            
            data.results.forEach(result => {
                if (result.success) {
                    updateItemStatus(result.item_id, 'uploaded');
                    successCount++;
                } else {
                    updateItemStatus(result.item_id, 'error');
                    failCount++;
                }
            });
            
            // Show results modal
            showBatchResults(data.results);
            
            // Hide progress after delay
            setTimeout(() => {
                progressContainer.style.display = 'none';
            }, 2000);
            
        } else {
            throw new Error(data.error || 'Batch upload failed');
        }
        
    } catch (error) {
        console.error('Error in batch upload:', error);
        showAlert('Batch upload failed: ' + error.message, 'danger');
        progressContainer.style.display = 'none';
    } finally {
        // Re-enable upload button
        uploadBtn.disabled = false;
        uploadBtn.innerHTML = '<i class="fas fa-cloud-upload-alt me-2"></i>Upload All Selected';
        updateUploadAllButton();
    }
}

// Show batch upload results
function showBatchResults(results) {
    const modalBody = document.getElementById('resultsModalBody');
    
    let successCount = results.filter(r => r.success).length;
    let failCount = results.length - successCount;
    
    let html = `
        <div class="row mb-3">
            <div class="col-md-6">
                <div class="card border-success">
                    <div class="card-body text-center">
                        <i class="fas fa-check-circle fa-2x text-success mb-2"></i>
                        <h4 class="text-success">${successCount}</h4>
                        <small class="text-muted">Successful</small>
                    </div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="card border-danger">
                    <div class="card-body text-center">
                        <i class="fas fa-exclamation-circle fa-2x text-danger mb-2"></i>
                        <h4 class="text-danger">${failCount}</h4>
                        <small class="text-muted">Failed</small>
                    </div>
                </div>
            </div>
        </div>
    `;
    
    if (results.length > 0) {
        html += `
            <h6>Detailed Results:</h6>
            <div class="table-responsive">
                <table class="table table-sm results-table">
                    <thead>
                        <tr>
                            <th>Item</th>
                            <th>Status</th>
                            <th>Error</th>
                        </tr>
                    </thead>
                    <tbody>
        `;
        
        results.forEach(result => {
            html += `
                <tr>
                    <td>${result.item_title || result.item_id}</td>
                    <td>
                        ${result.success ? 
                            '<span class="badge bg-success">Success</span>' : 
                            '<span class="badge bg-danger">Failed</span>'
                        }
                    </td>
                    <td>${result.error || '-'}</td>
                </tr>
            `;
        });
        
        html += `
                    </tbody>
                </table>
            </div>
        `;
    }
    
    modalBody.innerHTML = html;
    resultsModal.show();
}

// Update the existing updateUploadAllButton function
function updateUploadAllButton() {
    const uploadBtn = document.getElementById('uploadAllBtn');
    const selectedCountSpan = document.getElementById('selectedCount');
    const selectedCount = Object.keys(selectedPosters).length;
    
    // Update counter
    if (selectedCountSpan) {
        selectedCountSpan.textContent = selectedCount;
    }
    
    if (selectedCount > 0) {
        uploadBtn.disabled = false;
        uploadBtn.innerHTML = `<i class="fas fa-cloud-upload-alt me-2"></i>Upload All Selected (${selectedCount})`;
    } else {
        uploadBtn.disabled = true;
        uploadBtn.innerHTML = '<i class="fas fa-cloud-upload-alt me-2"></i>Upload All Selected';
    }
}

// Add filtering functions (already included in the HTML above)
// filterContent() and sortContent() are in the template



// Show alert message
function showAlert(message, type = 'info') {
    const alertHtml = `
        <div class="alert alert-${type} alert-dismissible fade show" role="alert">
            <i class="fas fa-info-circle me-2"></i>
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        </div>
    `;
    
    // Insert at top of container
    const container = document.querySelector('.container');
    container.insertAdjacentHTML('afterbegin', alertHtml);
    
    // Auto-hide after 5 seconds
    setTimeout(() => {
        const alert = container.querySelector('.alert');
        if (alert) {
            alert.remove();
        }
    }, 5000);
}

// Utility function to handle API errors
function handleApiError(error, defaultMessage = 'An error occurred') {
    console.error('API Error:', error);
    
    if (error.response) {
        // Server responded with error status
        return error.response.data?.error || `Server error: ${error.response.status}`;
    } else if (error.request) {
        // Request made but no response
        return 'Network error: Unable to connect to server';
    } else {
        // Something else happened
        return error.message || defaultMessage;
    }
}
