document.getElementById('copy-btn').addEventListener('click', async () => {
    const statusDiv = document.getElementById('status');
    const btn = document.getElementById('copy-btn');
    
    statusDiv.className = 'status';
    statusDiv.innerText = 'Lese Cookies...';
    btn.disabled = true;
    
    try {
        // Liste der gewünschten Cookies
        const targetCookies = ['reddit_session', 'loid', 'session_tracker', 'csrf_token', 'token_v2'];
        const cookiesList = [];
        
        for (const name of targetCookies) {
            try {
                // Wir fragen das Cookie für die Hauptdomain ab
                const cookie = await chrome.cookies.get({
                    url: 'https://www.reddit.com',
                    name: name
                });
                
                if (cookie && cookie.value) {
                    cookiesList.push(`${name}=${cookie.value}`);
                }
            } catch (err) {
                console.warn(`Fehler beim Lesen von Cookie ${name}:`, err);
            }
        }
        
        if (cookiesList.length === 0) {
            statusDiv.className = 'status error';
            statusDiv.innerText = 'Keine Reddit-Cookies gefunden. Bitte öffne reddit.com und logge dich ein.';
            btn.disabled = false;
            return;
        }
        
        // String zusammensetzen: name1=wert1; name2=wert2; ...
        const combinedString = cookiesList.join('; ');
        
        // In die Zwischenablage kopieren
        await navigator.clipboard.writeText(combinedString);
        
        statusDiv.className = 'status success';
        statusDiv.innerText = `Erfolgreich kopiert! (${cookiesList.length} Cookies)`;
        
        // Nach 2 Sekunden zurücksetzen
        setTimeout(() => {
            statusDiv.innerText = '';
            btn.disabled = false;
        }, 2000);
        
    } catch (error) {
        statusDiv.className = 'status error';
        statusDiv.innerText = `Fehler beim Kopieren: ${error.message}`;
        btn.disabled = false;
    }
});
