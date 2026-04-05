/* YouTube Community Analyzer — Minimal client-side helpers */

// Toggle password visibility
document.addEventListener('click', (e) => {
    const input = e.target.previousElementSibling;
    if (e.target.classList.contains('toggle-password') && input) {
        input.type = input.type === 'password' ? 'text' : 'password';
    }
});
