const photos = [...document.querySelectorAll(".photo img")];
const lightbox = document.querySelector("#lightbox");
const lightboxImage = lightbox?.querySelector(".lightbox-image");
const lightboxCount = lightbox?.querySelector(".lightbox-count");
const closeButton = lightbox?.querySelector(".lightbox-close");
const prevButton = lightbox?.querySelector(".lightbox-prev");
const nextButton = lightbox?.querySelector(".lightbox-next");

let currentIndex = 0;

function showPhoto(index) {
  currentIndex = (index + photos.length) % photos.length;
  const photo = photos[currentIndex];

  lightboxImage.src = photo.currentSrc || photo.src;
  lightboxImage.alt = photo.alt;
  lightboxCount.textContent = `${currentIndex + 1} / ${photos.length}`;
}

function openLightbox(index) {
  showPhoto(index);
  lightbox.hidden = false;
  document.body.classList.add("lightbox-open");
  closeButton.focus();
}

function closeLightbox() {
  lightbox.hidden = true;
  document.body.classList.remove("lightbox-open");
  lightboxImage.src = "";
  photos[currentIndex]?.closest(".photo")?.focus();
}

function moveLightbox(step) {
  showPhoto(currentIndex + step);
}

photos.forEach((photo, index) => {
  const frame = photo.closest(".photo");

  frame.tabIndex = 0;
  frame.setAttribute("role", "button");
  frame.setAttribute("aria-label", `${photo.alt} 크게 보기`);

  frame.addEventListener("click", () => openLightbox(index));
  frame.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openLightbox(index);
    }
  });
});

closeButton?.addEventListener("click", closeLightbox);
prevButton?.addEventListener("click", () => moveLightbox(-1));
nextButton?.addEventListener("click", () => moveLightbox(1));

lightbox?.addEventListener("click", (event) => {
  if (event.target === lightbox) {
    closeLightbox();
  }
});

document.addEventListener("keydown", (event) => {
  if (lightbox?.hidden) {
    return;
  }

  if (event.key === "Escape") {
    closeLightbox();
  } else if (event.key === "ArrowLeft") {
    moveLightbox(-1);
  } else if (event.key === "ArrowRight") {
    moveLightbox(1);
  }
});
