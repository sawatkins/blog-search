package handlers

import (
	"github.com/gofiber/fiber/v2"
)

func NotFound(c *fiber.Ctx) error {
	return c.Status(404).Render("404", fiber.Map{
		"Message": "404 Not found! Please try again",
	}, "layouts/main")
}

func Index(c *fiber.Ctx) error {
	return c.Render("index", fiber.Map{
		"Title":       "",
		"Canonical":   "",
		"Robots":      "index, follow",
		"Description": "",
		"Keywords":    "",
	}, "layouts/main")
}

func About(c *fiber.Ctx) error {
	return c.Render("about", fiber.Map{
		"Title":       "",
		"Canonical":   "",
		"Robots":      "index, follow",
		"Description": "",
		"Keywords":    "",
	}, "layouts/main")
}

func Search(c *fiber.Ctx) error {
	return c.Render("search", fiber.Map{
		"Title":       "",
		"Canonical":   "",
		"Robots":      "index, follow",
		"Description": "",
		"Keywords":    "",
	}, "layouts/main")
}
