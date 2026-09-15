document.addEventListener("click", async function (event) {

    // ==============================
    // OPEN HOW TO POPUP
    // ==============================

    if (event.target.closest("#howToBtn")) {

        try {

            // Check if popup was already loaded
            let overlay = document.getElementById("howToOverlay");

            if (!overlay) {

                // Load how-to.html
                const response = await fetch("how-to.html");

                if (!response.ok) {
                    throw new Error("Could not load the How To guide.");
                }

                const html = await response.text();

                // Create container
                const container = document.createElement("div");
                container.id = "howToContainer";
                container.innerHTML = html;

                document.body.appendChild(container);

                overlay = document.getElementById("howToOverlay");
            }


            if (!overlay) {
                throw new Error("How To popup was not found.");
            }


            // Open popup
            overlay.classList.add("show");

            // Stop background page scrolling
            document.body.style.overflow = "hidden";


            // Show correct guide
            showGuideByRole();


        } catch (error) {

            console.error("How To error:", error);

            alert("Could not open the How To guide: " + error.message);

        }

    }


    // ==============================
    // CLOSE USING X BUTTON
    // ==============================

    if (event.target.closest("#closeHowToBtn")) {
        closeHowToPopup();
    }


    // ==============================
    // CLOSE BY CLICKING BACKGROUND
    // ==============================

    if (
        event.target.id === "howToOverlay"
    ) {
        closeHowToPopup();
    }

});



/* =================================
   SHOW GUIDE BASED ON USER ROLE
================================= */

async function showGuideByRole() {

    const loading =
        document.getElementById("loadingGuide");

    const mentorGuide =
        document.getElementById("mentorGuide");

    const menteeGuide =
        document.getElementById("menteeGuide");

    const errorBox =
        document.getElementById("guideError");


    // Reset everything
    if (loading) {
        loading.hidden = false;
    }

    if (mentorGuide) {
        mentorGuide.hidden = true;
    }

    if (menteeGuide) {
        menteeGuide.hidden = true;
    }

    if (errorBox) {
        errorBox.hidden = true;
    }


    try {

        // Your existing pages already store this token
        const token =
            localStorage.getItem("wc_id_token");


        if (!token) {

            throw new Error(
                "Please log in to view the guide."
            );

        }


        const response = await fetch(
            "https://wordcomet.shyapsneon.tech/profile/me",
            {
                headers: {
                    "Authorization": `Bearer ${token}`
                }
            }
        );


        if (!response.ok) {

            throw new Error(
                "Could not load your WordComet profile."
            );

        }


        const profile = await response.json();


        if (loading) {
            loading.hidden = true;
        }


        // ==========================
        // MENTOR
        // ==========================

        if (profile.role === "mentor") {

            mentorGuide.hidden = false;
            menteeGuide.hidden = true;

        }


        // ==========================
        // MENTEE
        // ==========================

        else if (profile.role === "mentee") {

            mentorGuide.hidden = true;
            menteeGuide.hidden = false;

        }


        // ==========================
        // NO ROLE
        // ==========================

        else {

            throw new Error(
                "Please complete your WordComet profile first."
            );

        }


    } catch (error) {

        console.error("Guide profile error:", error);


        if (loading) {
            loading.hidden = true;
        }

        if (mentorGuide) {
            mentorGuide.hidden = true;
        }

        if (menteeGuide) {
            menteeGuide.hidden = true;
        }


        if (errorBox) {

            errorBox.textContent = error.message;
            errorBox.hidden = false;

        }

    }

}



/* =================================
   CLOSE POPUP
================================= */

function closeHowToPopup() {

    const overlay =
        document.getElementById("howToOverlay");


    if (overlay) {
        overlay.classList.remove("show");
    }


    document.body.style.overflow = "";

}



/* =================================
   CLOSE WITH ESC KEY
================================= */

document.addEventListener("keydown", function (event) {

    if (event.key === "Escape") {

        closeHowToPopup();

    }

});