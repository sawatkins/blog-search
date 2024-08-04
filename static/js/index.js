window.onload = function () {
    document.getElementById("search-form").addEventListener("submit", handleSearchInput);
}

function handleSearchInput(event) {
    event.preventDefault(); 
    const searchQuery = document.getElementById("search-input").value;
    if (searchQuery.trim() === '') {
        return;
    }
    let params = new URLSearchParams();
    params.append("q", searchQuery)
    window.location.href = "/search?" + params
}
