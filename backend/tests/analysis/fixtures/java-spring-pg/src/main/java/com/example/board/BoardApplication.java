package com.example.board;

import java.util.List;

import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.Id;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

@SpringBootApplication
@RestController
public class BoardApplication {

    private final PostRepository posts;

    @Value("${app.admin-token}")
    private String adminToken;

    BoardApplication(PostRepository posts) {
        this.posts = posts;
    }

    public static void main(String[] args) {
        SpringApplication.run(BoardApplication.class, args);
    }

    @GetMapping("/posts")
    List<Post> list() {
        return posts.findAll();
    }

    @PostMapping("/posts")
    Post create(@RequestHeader("Authorization") String auth, @RequestBody Post post) {
        if (!auth.equals("Bearer " + adminToken)) {
            throw new IllegalArgumentException("forbidden");
        }
        return posts.save(post);
    }
}

@Entity
class Post {
    @Id
    @GeneratedValue
    public Long id;
    public String title;
}

interface PostRepository extends JpaRepository<Post, Long> {
}
