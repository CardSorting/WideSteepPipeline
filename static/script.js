document.addEventListener('DOMContentLoaded', function() {
    'use strict';

    // DOM Elements
    const cardNamesTextarea = document.getElementById('card-names');
    const fetchButton = document.getElementById('fetch-button');
    const exportButton = document.getElementById('export-button');
    const clearButton = document.getElementById('clear-button');
    const statusContainer = document.getElementById('status-container');
    const progressContainer = document.getElementById('progress-container');
    const progressFill = document.getElementById('progress-fill');
    const messageContainer = document.getElementById('message-container');
    const cardList = document.getElementById('card-list');

    // Check if all elements are found
    if (!cardNamesTextarea || !fetchButton || !exportButton || !clearButton || 
        !statusContainer || !progressContainer || !progressFill || 
        !messageContainer || !cardList) {
        console.error('One or more DOM elements not found. Check your HTML IDs.');
        return;  // Exit the script if any element is missing
    }

    let isFetching = false;
    let totalCards = 0;
    let fetchedCards = 0;

    // Utility Functions
    function displayMessage(message, type) {
        const messageElement = document.createElement('div');
        messageElement.textContent = message;
        messageElement.className = type;
        messageContainer.appendChild(messageElement);
        setTimeout(() => messageElement.remove(), 5000);
    }

    function updateProgress() {
        const progress = (fetchedCards / totalCards) * 100;
        progressFill.style.width = `${progress}%`;
        statusContainer.textContent = `Fetched ${fetchedCards} of ${totalCards} cards`;

        if (isFetching) {
            progressContainer.style.display = 'block';
            fetchButton.disabled = true;
            exportButton.disabled = true;
            clearButton.disabled = true;
        } else {
            progressContainer.style.display = 'none';
            fetchButton.disabled = false;
            exportButton.disabled = fetchedCards === 0;
            clearButton.disabled = fetchedCards === 0;
            if (fetchedCards > 0 && fetchedCards === totalCards) {
                displayMessage('Card fetching complete!', 'success');
            }
        }
    }

    function createCardHtml(card) {
        return `
            <div class="card-item">
                <h3>${escapeHtml(card.name)}</h3>
                <p><strong>Oracle Text:</strong> ${escapeHtml(card.oracle_text)}</p>
                <p><strong>Mana Cost:</strong> ${escapeHtml(card.mana_cost)}</p>
                <p><strong>Type:</strong> ${escapeHtml(card.type_line)}</p>
                <p><strong>Set:</strong> ${escapeHtml(card.set_name)}</p>
                <p><strong>Status:</strong> ${escapeHtml(card.status)}</p>
            </div>
        `;
    }

    function escapeHtml(unsafe) {
        return unsafe
            ? unsafe
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;")
            : '';
    }

    // API Interaction Functions
    async function fetchCards(cardNames) {
        try {
            console.log('Sending fetch request with card names:', cardNames);
            const response = await fetch('/fetch', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ card_names: cardNames }),
            });
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const data = await response.json();
            console.log('Received response from server:', data);
            return data;
        } catch (error) {
            console.error('Error in fetchCards:', error);
            throw error;
        }
    }

    async function getStatus() {
        try {
            const response = await fetch('/status');
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return await response.json();
        } catch (error) {
            console.error('Error in getStatus:', error);
            throw error;
        }
    }

    // Main Functions
    async function handleFetchClick() {
        if (isFetching) return;

        const cardNames = cardNamesTextarea.value.split('\n').filter(name => name.trim() !== '');

        if (cardNames.length === 0) {
            displayMessage('Please enter at least one card name.', 'error');
            return;
        }

        isFetching = true;
        totalCards = cardNames.length;
        fetchedCards = 0;
        updateProgress();

        try {
            const data = await fetchCards(cardNames);
            console.log('Fetch response data:', data);
            if (Array.isArray(data)) {
                displayMessage(`Queued ${data.length} cards for fetching.`, 'success');
                updateCardList(data);
                pollStatus();
            } else {
                throw new Error('Unexpected response format from server');
            }
        } catch (error) {
            console.error('Error:', error);
            displayMessage('Error fetching cards. Please try again.', 'error');
        } finally {
            isFetching = false;
            updateProgress();
        }

        cardNamesTextarea.value = '';
    }

    async function pollStatus() {
        try {
            const status = await getStatus();
            console.log('Status:', status);
            fetchedCards = status.cache_size;
            updateProgress();
            if (status.queue_size > 0 || status.is_fetching) {
                await updateCardList();
                setTimeout(pollStatus, 1000);
            } else {
                isFetching = false;
                updateProgress();
            }
        } catch (error) {
            console.error('Error in pollStatus:', error);
            displayMessage('Error updating status.', 'error');
            isFetching = false;
            updateProgress();
        }
    }

    async function updateCardList() {
        try {
            const cards = await fetchCards([]);  // Fetch all cached cards
            cardList.innerHTML = cards.map(card => {
                if (card.status === 'queued' || card.status === 'queue full') {
                    return `<div class="card-item">
                        <h3>${escapeHtml(card.name)}</h3>
                        <p><strong>Status:</strong> ${escapeHtml(card.status)}</p>
                    </div>`;
                } else {
                    return createCardHtml(card);
                }
            }).join('');
        } catch (error) {
            console.error('Error updating card list:', error);
            displayMessage('Error updating card list.', 'error');
        }
    }

    async function handleExportClick() {
        try {
            const response = await fetch('/export', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ card_names: [] }),  // We're exporting all cached cards
            });
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;
            a.download = 'card_data.csv';
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
        } catch (error) {
            console.error('Error:', error);
            displayMessage('Error exporting cards. Please try again.', 'error');
        }
    }

    async function handleClearClick() {
        try {
            const response = await fetch('/clear', { method: 'POST' });
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            cardList.innerHTML = '';
            fetchedCards = 0;
            totalCards = 0;
            updateProgress();
            displayMessage('All cards cleared.', 'success');
        } catch (error) {
            console.error('Error clearing cards:', error);
            displayMessage('Error clearing cards. Please try again.', 'error');
        }
    }

    // Event Listeners
    fetchButton.addEventListener('click', handleFetchClick);
    exportButton.addEventListener('click', handleExportClick);
    clearButton.addEventListener('click', handleClearClick);

    // Initial setup
    pollStatus();
});